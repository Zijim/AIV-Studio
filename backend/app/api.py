import asyncio
import json
import os
import shutil

import httpx
from fastapi import APIRouter, Body, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import String, and_, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError

from .config import ROOT
from .conversations import resolve_conversation
from .creative_agent_scope import protect_active
from .errors import AppError
from .media_delivery import file_etag, media_response, small_preview
from .models import (
    Artifact,
    Asset,
    AssetRelation,
    Blob,
    CreativeAgentCall,
    CreativeAgentRun,
    Event,
    Execution,
    ImportBatch,
    Maintenance,
    Message,
    Plan,
    Project,
    ProjectAsset,
    ProjectAssetTag,
    ProjectTag,
    Setting,
    Task,
    now,
    record_event,
    row_dict,
    uid,
)
from .plans import ACTIVE_TASKS, require_project
from .schemas import (
    AssetNote,
    AssetTags,
    ExecutePlan,
    KeyAssetsRemove,
    KeyAssetsRequest,
    ManualPlan,
    MessageRequest,
    PlanPatch,
    ProjectCreate,
    ProjectUpdate,
    TagAssets,
    TagCreate,
    TagPatch,
)
from .task_identity import execution_identity
from .task_media import resolve_task_media
from .task_timing import execution_timing
from .worker import TERMINAL

router = APIRouter(prefix="/api/v1")


def svc(request):
    return request.app.state


TAG_PALETTE = ("#e6a16e", "#8fb7a8", "#b6a1d8", "#d9a3a3", "#9fb8d9", "#c9bd8e", "#a8c28f", "#c39bc4")
TAG_LIMIT = 100


def clean_tag_name(value):
    name = " ".join(value.split())[:24]
    if not name:
        raise AppError("tag_name", "标签名不能为空。")
    return name


def library_kind(asset):
    if asset.origin == "imported":
        return "imported"
    if asset.role == "intermediate":
        return "intermediate"
    if asset.role == "preview":
        return "preview"
    if (asset.source_info or {}).get("internal_preparation"):
        return "preparation"
    return "final"


async def require_tag(session, project_id, tag_id):
    tag = await session.get(ProjectTag, tag_id)
    if not tag or tag.project_id != project_id:
        raise AppError("tag_not_found", "标签不存在。", 404)
    return tag


async def tag_counts(session, project_id):
    rows = (
        await session.execute(
            select(ProjectAssetTag.tag_id, func.count())
            .join(
                ProjectAsset,
                and_(
                    ProjectAsset.project_id == ProjectAssetTag.project_id,
                    ProjectAsset.asset_id == ProjectAssetTag.asset_id,
                ),
            )
            .where(
                ProjectAssetTag.project_id == project_id,
                ProjectAsset.curated_at.is_not(None),
                ProjectAsset.removed_at.is_(None),
            )
            .group_by(ProjectAssetTag.tag_id)
        )
    ).all()
    return {tag_id: count for tag_id, count in rows}


async def project_tag_views(session, project_id):
    tags = list(
        await session.scalars(
            select(ProjectTag).where(ProjectTag.project_id == project_id).order_by(ProjectTag.position, ProjectTag.name)
        )
    )
    counts = await tag_counts(session, project_id)
    return [{**row_dict(tag), "count": counts.get(tag.id, 0)} for tag in tags]


async def create_project_tag(session, project_id, name, color=None):
    name = clean_tag_name(name)
    existing = await session.scalar(
        select(ProjectTag).where(
            ProjectTag.project_id == project_id,
            ProjectTag.name.collate("NOCASE") == name,
        )
    )
    if existing:
        raise AppError("tag_exists", "同名标签已存在。", 409)
    tags = list(await session.scalars(select(ProjectTag).where(ProjectTag.project_id == project_id)))
    if len(tags) >= TAG_LIMIT:
        raise AppError("tag_limit", f"每个项目最多{TAG_LIMIT}个标签。", 409)
    position = max((tag.position for tag in tags), default=-1) + 1
    tag = ProjectTag(
        id=uid(),
        project_id=project_id,
        name=name,
        color=color or TAG_PALETTE[position % len(TAG_PALETTE)],
        position=position,
    )
    session.add(tag)
    await session.flush()
    return tag


async def validate_tag_ids(session, project_id, tag_ids):
    ids = list(dict.fromkeys(tag_ids))
    if not ids:
        return []
    tags = list(
        await session.scalars(select(ProjectTag).where(ProjectTag.project_id == project_id, ProjectTag.id.in_(ids)))
    )
    if len(tags) != len(ids):
        raise AppError("tag_not_found", "标签不属于当前项目或已删除。", 404)
    return tags


async def set_asset_tags(session, storage, project_id, asset_id, tag_ids):
    membership = await session.get(ProjectAsset, (project_id, asset_id))
    if not membership or membership.removed_at:
        raise AppError("asset_not_found", "项目素材不存在。", 404)
    tags = await validate_tag_ids(session, project_id, tag_ids)
    await session.execute(
        delete(ProjectAssetTag).where(
            ProjectAssetTag.project_id == project_id,
            ProjectAssetTag.asset_id == asset_id,
        )
    )
    for tag in tags:
        session.add(
            ProjectAssetTag(project_id=project_id, asset_id=asset_id, tag_id=tag.id, created_at=now())
        )
    if tags and not membership.curated_at:
        membership.curated_at = now()
    asset = await session.get(Asset, asset_id)
    await storage.index(session, project_id, asset)
    return membership


async def add_asset_tags(session, storage, project_id, asset_ids, tag_ids):
    tags = await validate_tag_ids(session, project_id, tag_ids)
    for asset_id in asset_ids:
        membership = await session.get(ProjectAsset, (project_id, asset_id))
        if not membership or membership.removed_at:
            raise AppError("asset_not_found", "项目素材不存在。", 404)
        if not membership.curated_at:
            membership.curated_at = now()
        existing = set(
            await session.scalars(
                select(ProjectAssetTag.tag_id).where(
                    ProjectAssetTag.project_id == project_id,
                    ProjectAssetTag.asset_id == asset_id,
                )
            )
        )
        for tag in tags:
            if tag.id not in existing:
                session.add(
                    ProjectAssetTag(project_id=project_id, asset_id=asset_id, tag_id=tag.id, created_at=now())
                )
                existing.add(tag.id)
        asset = await session.get(Asset, asset_id)
        await storage.index(session, project_id, asset)


@router.get("/session")
async def session(request: Request):
    from fastapi.responses import JSONResponse

    async with svc(request).db.session() as s:
        cursor = await s.scalar(select(func.max(Event.id))) or 0
    public = request.state.public_access
    response = JSONResponse(
        {
            "csrf_token": svc(request).public_csrf if public else svc(request).csrf,
            "version": "0.1.0",
            "event_cursor": cursor,
            "public_access": public,
            "can_manage": not public,
            "max_upload_bytes": svc(request).settings.public_max_request_bytes if public else None,
        }
    )
    response.set_cookie(
        "aiv_session",
        svc(request).public_session_token if public else svc(request).session_token,
        httponly=True,
        samesite="strict",
        secure=public,
    )
    return response


@router.get("/health")
async def health(request: Request):
    state = svc(request)
    if request.state.public_access:
        return {
            "status": "ok",
            "version": "0.1.0",
            "comfyui": {key: state.worker.status.get(key) for key in ("connected", "compatible", "version")},
            "planner": {"configured": bool(await state.planner.key()), "model": state.settings.llm_model},
        }
    return {
        "status": "ok",
        "version": "0.1.0",
        "workspace_root": str(ROOT),
        "data_dir": str(state.settings.data_dir),
        "process_id": os.getpid(),
        "comfyui": state.worker.status,
        "planner": {
            "configured": bool(await state.planner.key()),
            "model": state.settings.llm_model,
            "vision_max_images": state.settings.vision_max_images,
            "vision_max_edge": state.settings.vision_max_edge,
        },
        "storage": {
            "free_bytes": shutil.disk_usage(state.settings.data_dir).free,
            "minimum_free_bytes": state.settings.minimum_free_bytes,
        },
    }


@router.get("/tools")
async def tools(request: Request):
    state = svc(request)
    cards = {card["id"]: card for card in state.planner.guides.cards(state.registry, state.settings)}
    return [
        {
            **tool,
            "assistant_guidance": {
                "available": cards[tool["id"]]["assistant_available"],
                "task_mode": cards[tool["id"]]["task_mode"],
                "issue": cards[tool["id"]]["guide_issue"],
            },
        }
        for tool in state.registry.public()
    ]


@router.post("/system/shutdown")
async def shutdown(request: Request):
    callback = getattr(svc(request), "request_shutdown", None)
    if not callback:
        raise AppError("shutdown_unavailable", "当前为开发服务器，请通过启动终端关闭。", 409)
    asyncio.get_running_loop().call_later(0.2, callback)
    return {"shutdown_requested": True}


@router.post("/tools/refresh")
async def refresh_tools(request: Request):
    await svc(request).worker.health(force=True)
    return svc(request).registry.public()


@router.get("/projects")
async def projects(request: Request, removed: bool = False):
    async with svc(request).db.session() as s:
        query = select(Project).order_by(Project.updated_at.desc())
        query = query.where(Project.removed_at.is_not(None) if removed else Project.removed_at.is_(None))
        return [row_dict(p) for p in (await s.scalars(query)).all()]


@router.post("/projects", status_code=201)
async def create_project(request: Request, body: ProjectCreate):
    async with svc(request).db.transaction() as s:
        project = Project(**body.model_dump())
        s.add(project)
        await s.flush()
        await resolve_conversation(s, project.id)
        record_event(s, "project.updated", project.id, project.id)
        return row_dict(project)


@router.get("/projects/{project_id}")
async def project(request: Request, project_id: str):
    async with svc(request).db.session() as s:
        return row_dict(await require_project(s, project_id))


@router.patch("/projects/{project_id}")
async def edit_project(request: Request, project_id: str, body: ProjectUpdate):
    async with svc(request).db.transaction() as s:
        project = await require_project(s, project_id, writable=True)
        if project.revision != body.revision:
            raise AppError("revision_conflict", "项目已修改，请刷新。", 409)
        for k, v in body.model_dump(exclude={"revision"}, exclude_none=True).items():
            setattr(project, k, v)
        project.revision += 1
        project.updated_at = now()
        for asset in (
            await s.scalars(select(Asset).join(ProjectAsset).where(ProjectAsset.project_id == project_id))
        ).all():
            await svc(request).storage.index(s, project_id, asset)
        record_event(s, "project.updated", project_id, project_id)
        return row_dict(project)


@router.post("/projects/{project_id}/archive")
async def archive_project(request: Request, project_id: str, archived: bool = Body(default=True, embed=True)):
    async with svc(request).db.transaction() as s:
        p = await require_project(s, project_id)
        if archived:
            await protect_active(s, project_id=project_id)
        active = await s.scalar(
            select(Task).where(Task.project_id == project_id, Task.state.in_(ACTIVE_TASKS)).limit(1)
        )
        if active:
            raise AppError("project_busy", "请等待项目任务结束再归档。", 409)
        p.archived_at = now() if archived else None
        p.updated_at = now()
        record_event(s, "project.updated", project_id, project_id)
        return row_dict(p)


@router.delete("/projects/{project_id}")
async def remove_project(request: Request, project_id: str):
    async with svc(request).db.transaction() as s:
        p = await require_project(s, project_id)
        await protect_active(s, project_id=project_id)
        active = await s.scalar(
            select(Task).where(Task.project_id == project_id, Task.state.in_(ACTIVE_TASKS)).limit(1)
        )
        if active:
            raise AppError("project_busy", "请等待或取消任务后再移除项目。", 409)
        p.removed_at = now()
        record_event(s, "project.updated", project_id, project_id)
        return {"removed": True}


@router.post("/projects/{project_id}/restore")
async def restore_project(request: Request, project_id: str):
    async with svc(request).db.transaction() as s:
        p = await s.get(Project, project_id)
        if not p:
            raise AppError("project_not_found", "项目不存在。", 404)
        p.removed_at = None
        p.updated_at = now()
        record_event(s, "project.updated", project_id, project_id)
        return row_dict(p)


@router.get("/assets")
@router.get("/projects/{project_id}/assets")
async def assets(
    request: Request,
    project_id: str | None = None,
    q: str = "",
    media_type: str | None = None,
    curated: bool | None = None,
    tag_id: str | None = None,
    origin: str | None = None,
    role: str | None = None,
    best: bool = False,
    sort: str = "created",
    removed: bool = False,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cursor: str | None = None,
):
    if cursor is not None:
        if not cursor.isdecimal() or len(cursor) > 10:
            raise AppError("cursor_invalid", "分页位置无效。")
        offset = int(cursor)
    if origin and origin not in ("imported", "generated"):
        raise AppError("filter_invalid", "素材来源筛选无效。")
    if role and role not in ("imported", "final", "preview", "intermediate"):
        raise AppError("filter_invalid", "素材角色筛选无效。")
    if sort not in ("created", "curated", "name"):
        raise AppError("sort_invalid", "排序方式无效。")
    if best and not project_id:
        raise AppError("filter_invalid", "最佳候选筛选需要指定项目。")
    state = svc(request)
    async with state.db.session() as s:
        query = (
            select(Asset, ProjectAsset)
            .join(ProjectAsset)
            .join(Project, Project.id == ProjectAsset.project_id)
            .where(Project.removed_at.is_(None))
        )
        if project_id:
            await require_project(s, project_id)
            query = query.where(ProjectAsset.project_id == project_id)
        query = query.where(ProjectAsset.removed_at.is_not(None) if removed else ProjectAsset.removed_at.is_(None))
        if media_type:
            query = query.where(Asset.media_type == media_type)
        if curated is True:
            query = query.where(ProjectAsset.curated_at.is_not(None))
        elif curated is False:
            query = query.where(ProjectAsset.curated_at.is_(None))
        if tag_id:
            if not project_id:
                raise AppError("filter_invalid", "标签筛选需要指定项目。")
            tag = await s.get(ProjectTag, tag_id)
            if not tag or tag.project_id != project_id:
                raise AppError("tag_not_found", "标签不存在。", 404)
            query = query.join(
                ProjectAssetTag,
                and_(
                    ProjectAssetTag.project_id == ProjectAsset.project_id,
                    ProjectAssetTag.asset_id == Asset.id,
                ),
            ).where(ProjectAssetTag.tag_id == tag_id)
        if origin:
            query = query.where(Asset.origin == origin)
        if role:
            query = query.where(Asset.role == role)
        if best:
            query = query.where(
                Asset.id.in_(select(Task.best_asset_id).where(Task.project_id == project_id, Task.best_asset_id.is_not(None)))
            )
        if q.strip():
            term = q.strip().casefold()[:200]
            if len(term) >= 3:
                sql = "SELECT project_id,asset_id FROM asset_search WHERE asset_search MATCH :term"
                match_value = '"' + term.replace('"', '""') + '"'
            else:
                sql = "SELECT project_id,asset_id FROM asset_search WHERE body LIKE :term ESCAPE '\\'"
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                match_value = "%" + escaped + "%"
            matches = (
                text(sql).bindparams(term=match_value).columns(project_id=String, asset_id=String).subquery("matches")
            )
            query = query.join(
                matches, (matches.c.asset_id == Asset.id) & (matches.c.project_id == ProjectAsset.project_id)
            )
        total = await s.scalar(select(func.count()).select_from(query.subquery()))
        ordering = {
            "created": (Asset.created_at.desc(), Asset.id.desc()),
            "curated": (ProjectAsset.curated_at.desc(), Asset.created_at.desc()),
            "name": (Asset.name.asc(), Asset.created_at.desc()),
        }[sort]
        rows = (await s.execute(query.order_by(*ordering).offset(offset).limit(limit))).all()
        tags = {}
        grouped = {}
        for asset, membership in rows:
            grouped.setdefault(membership.project_id, []).append(asset.id)
        for pid, asset_ids in grouped.items():
            tags.update(await state.storage.tags_for(s, pid, asset_ids))
        best_ids = {
            value
            for value in await s.scalars(
                select(Task.best_asset_id).where(Task.best_asset_id.in_([a.id for a, _ in rows]))
            )
            if value
        }
        items = []
        for asset, membership in rows:
            view = await state.storage.asset_view(s, asset, membership, tags=tags)
            view["kind"] = library_kind(asset)
            view["best"] = asset.id in best_ids
            items.append(view)
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "next_cursor": str(offset + limit) if offset + limit < total else None,
        }


@router.post("/projects/{project_id}/imports")
async def import_files(request: Request, project_id: str, files: list[UploadFile] = File(...)):
    state = svc(request)
    if len(files) > 50:
        raise AppError("import_count", "每批最多导入50个文件。")
    async with state.db.transaction() as s:
        await require_project(s, project_id, writable=True)
        batch = ImportBatch(id=uid(), project_id=project_id)
        s.add(batch)
    items = []
    for upload in files:
        try:
            staged = await state.storage.stage_upload(upload)
            asset_id = await state.storage.ingest(
                staged, upload.filename or "未命名文件", project_id, source_info={"original_filename": upload.filename}
            )
            items.append({"name": upload.filename, "state": "saved", "asset_id": asset_id})
        except Exception as exc:
            items.append(
                {
                    "name": upload.filename,
                    "state": "failed",
                    "error": getattr(exc, "message", "") or f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            await upload.close()
        async with state.db.transaction() as s:
            current = await s.get(ImportBatch, batch.id)
            current.items = list(items)
    async with state.db.transaction() as s:
        current = await s.get(ImportBatch, batch.id)
        current.state = "completed" if all(i["state"] == "saved" for i in items) else "partial"
        record_event(s, "import.completed", project_id, batch.id)
        return row_dict(current)


@router.get("/imports/{batch_id}")
async def import_status(request: Request, batch_id: str):
    async with svc(request).db.session() as s:
        batch = await s.get(ImportBatch, batch_id)
        if not batch:
            raise AppError("import_not_found", "导入记录不存在。", 404)
        return row_dict(batch)


@router.post("/projects/{project_id}/assets/{asset_id}/attach")
async def attach_asset(request: Request, project_id: str, asset_id: str):
    async with svc(request).db.transaction() as s:
        await require_project(s, project_id, writable=True)
        asset = await s.get(Asset, asset_id)
        if not asset:
            raise AppError("asset_not_found", "素材不存在。", 404)
        membership = await s.get(ProjectAsset, (project_id, asset_id))
        if membership:
            if membership.removed_at:
                membership.removed_at = None
                membership.revision += 1
        else:
            s.add(ProjectAsset(project_id=project_id, asset_id=asset_id, usage="reference"))
        await svc(request).storage.index(s, project_id, asset)
        record_event(s, "asset.saved", project_id, asset_id)
        return {"asset_id": asset_id}


@router.patch("/projects/{project_id}/assets/{asset_id}")
async def edit_note(request: Request, project_id: str, asset_id: str, body: AssetNote):
    async with svc(request).db.transaction() as s:
        p = await require_project(s, project_id, writable=True)
        m = await s.get(ProjectAsset, (project_id, asset_id))
        if not m or m.removed_at:
            raise AppError("asset_not_found", "项目素材不存在。", 404)
        if body.revision is not None and body.revision != m.revision:
            raise AppError("revision_conflict", "素材信息已变化，请刷新。", 409)
        if body.name is not None:
            value = " ".join(body.name.split())[:120]
            m.display_name = value or None
        if body.note is not None:
            m.note = body.note
        if body.usage is not None:
            m.usage = body.usage
        if body.tags is not None:
            tag_ids = []
            for value in body.tags:
                name = clean_tag_name(value)
                tag = await s.scalar(
                    select(ProjectTag).where(
                        ProjectTag.project_id == project_id,
                        ProjectTag.name.collate("NOCASE") == name,
                    )
                )
                if not tag:
                    tag = await create_project_tag(s, project_id, name)
                tag_ids.append(tag.id)
            await set_asset_tags(s, svc(request).storage, project_id, asset_id, tag_ids)
        m.revision += 1
        p.revision += 1
        await svc(request).storage.index(s, project_id, await s.get(Asset, asset_id))
        record_event(s, "asset.updated", project_id, asset_id)
        return row_dict(m)


@router.delete("/projects/{project_id}/assets/{asset_id}")
async def unlink_asset(request: Request, project_id: str, asset_id: str):
    async with svc(request).db.transaction() as s:
        await require_project(s, project_id, writable=True)
        await protect_active(s, project_id=project_id, asset_id=asset_id)
        m = await s.get(ProjectAsset, (project_id, asset_id))
        if not m:
            raise AppError("asset_not_found", "项目素材不存在。", 404)
        m.removed_at = now()
        m.revision += 1
        record_event(s, "asset.updated", project_id, asset_id)
        return {"removed": True}


@router.post("/projects/{project_id}/assets/{asset_id}/restore")
async def restore_asset(request: Request, project_id: str, asset_id: str):
    return await attach_asset(request, project_id, asset_id)


@router.get("/projects/{project_id}/library/facets")
async def library_facets(request: Request, project_id: str):
    state = svc(request)
    async with state.db.session() as s:
        await require_project(s, project_id)
        active = (ProjectAsset.project_id == project_id, ProjectAsset.removed_at.is_(None))
        all_count = await s.scalar(select(func.count()).select_from(ProjectAsset).where(*active)) or 0
        key_count = (
            await s.scalar(
                select(func.count()).select_from(ProjectAsset).where(*active, ProjectAsset.curated_at.is_not(None))
            )
            or 0
        )
        trash_count = (
            await s.scalar(
                select(func.count())
                .select_from(ProjectAsset)
                .where(ProjectAsset.project_id == project_id, ProjectAsset.removed_at.is_not(None))
            )
            or 0
        )
        origins = dict(
            (
                await s.execute(
                    select(Asset.origin, func.count())
                    .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                    .where(*active)
                    .group_by(Asset.origin)
                )
            ).all()
        )
        roles = dict(
            (
                await s.execute(
                    select(Asset.role, func.count())
                    .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                    .where(*active)
                    .group_by(Asset.role)
                )
            ).all()
        )
        media_types = dict(
            (
                await s.execute(
                    select(Asset.media_type, func.count())
                    .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                    .where(*active)
                    .group_by(Asset.media_type)
                )
            ).all()
        )
        best = (
            await s.scalar(
                select(func.count()).select_from(Task).where(Task.project_id == project_id, Task.best_asset_id.is_not(None))
            )
            or 0
        )
        return {
            "all": all_count,
            "key": key_count,
            "uncurated": all_count - key_count,
            "trash": trash_count,
            "best": best,
            "origins": origins,
            "roles": roles,
            "media_types": media_types,
            "tags": await project_tag_views(s, project_id),
        }


@router.get("/projects/{project_id}/tags")
async def list_tags(request: Request, project_id: str):
    async with svc(request).db.session() as s:
        await require_project(s, project_id)
        return await project_tag_views(s, project_id)


@router.post("/projects/{project_id}/tags", status_code=201)
async def create_tag(request: Request, project_id: str, body: TagCreate):
    state = svc(request)
    async with state.db.transaction() as s:
        await require_project(s, project_id, writable=True)
        try:
            tag = await create_project_tag(s, project_id, body.name, body.color)
        except IntegrityError as exc:
            raise AppError("tag_exists", "同名标签已存在。", 409) from exc
        record_event(s, "library.updated", project_id, tag.id, action="tag.create", tag_id=tag.id)
        return {**row_dict(tag), "count": 0}


@router.patch("/projects/{project_id}/tags/{tag_id}")
async def edit_tag(request: Request, project_id: str, tag_id: str, body: TagPatch):
    state = svc(request)
    async with state.db.transaction() as s:
        await require_project(s, project_id, writable=True)
        tag = await require_tag(s, project_id, tag_id)
        renamed = False
        if body.name is not None:
            name = clean_tag_name(body.name)
            if name.casefold() != tag.name.casefold():
                duplicate = await s.scalar(
                    select(ProjectTag).where(
                        ProjectTag.project_id == project_id,
                        ProjectTag.id != tag.id,
                        ProjectTag.name.collate("NOCASE") == name,
                    )
                )
                if duplicate:
                    raise AppError("tag_exists", "同名标签已存在。", 409)
                tag.name = name
                renamed = True
        if body.color is not None:
            tag.color = body.color
        if body.position is not None:
            tag.position = body.position
        if renamed:
            asset_ids = list(
                await s.scalars(
                    select(ProjectAssetTag.asset_id).where(
                        ProjectAssetTag.project_id == project_id, ProjectAssetTag.tag_id == tag.id
                    )
                )
            )
            for asset_id in asset_ids:
                asset = await s.get(Asset, asset_id)
                if asset:
                    await state.storage.index(s, project_id, asset)
        record_event(s, "library.updated", project_id, tag.id, action="tag.update", tag_id=tag.id)
        counts = await tag_counts(s, project_id)
        return {**row_dict(tag), "count": counts.get(tag.id, 0)}


@router.delete("/projects/{project_id}/tags/{tag_id}")
async def delete_tag(request: Request, project_id: str, tag_id: str):
    state = svc(request)
    async with state.db.transaction() as s:
        await require_project(s, project_id, writable=True)
        tag = await require_tag(s, project_id, tag_id)
        asset_ids = list(
            await s.scalars(
                select(ProjectAssetTag.asset_id).where(
                    ProjectAssetTag.project_id == project_id, ProjectAssetTag.tag_id == tag.id
                )
            )
        )
        await s.execute(
            delete(ProjectAssetTag).where(
                ProjectAssetTag.project_id == project_id, ProjectAssetTag.tag_id == tag.id
            )
        )
        await s.delete(tag)
        for asset_id in asset_ids:
            asset = await s.get(Asset, asset_id)
            if asset:
                await state.storage.index(s, project_id, asset)
        record_event(s, "library.updated", project_id, tag_id, action="tag.delete", tag_id=tag_id)
        return {"deleted": True, "asset_ids": asset_ids}


@router.post("/projects/{project_id}/tags/{tag_id}/assets")
async def tag_assets(request: Request, project_id: str, tag_id: str, body: TagAssets):
    state = svc(request)
    async with state.db.transaction() as s:
        await require_project(s, project_id, writable=True)
        tag = await require_tag(s, project_id, tag_id)
        await add_asset_tags(s, state.storage, project_id, body.asset_ids, [tag.id])
        record_event(
            s,
            "library.updated",
            project_id,
            tag.id,
            action="tag.assets",
            tag_id=tag.id,
            asset_ids=body.asset_ids,
        )
        return {"tag_id": tag.id, "asset_ids": body.asset_ids}


@router.put("/projects/{project_id}/assets/{asset_id}/tags")
async def set_tags(request: Request, project_id: str, asset_id: str, body: AssetTags):
    state = svc(request)
    async with state.db.transaction() as s:
        await require_project(s, project_id, writable=True)
        await set_asset_tags(s, state.storage, project_id, asset_id, body.tag_ids)
        record_event(
            s,
            "library.updated",
            project_id,
            asset_id,
            action="asset.tags",
            asset_id=asset_id,
            tag_ids=body.tag_ids,
        )
        return {"asset_id": asset_id, "tag_ids": body.tag_ids}


@router.post("/projects/{project_id}/key-assets")
async def add_key_assets(request: Request, project_id: str, body: KeyAssetsRequest):
    state = svc(request)
    async with state.db.transaction() as s:
        await require_project(s, project_id, writable=True)
        await add_asset_tags(s, state.storage, project_id, body.asset_ids, body.tag_ids)
        record_event(
            s,
            "library.updated",
            project_id,
            body.asset_ids[0],
            action="key.add",
            asset_ids=body.asset_ids,
            tag_ids=body.tag_ids,
        )
        return {"asset_ids": body.asset_ids, "tag_ids": body.tag_ids}


@router.post("/projects/{project_id}/key-assets/remove")
async def remove_key_assets(request: Request, project_id: str, body: KeyAssetsRemove):
    state = svc(request)
    async with state.db.transaction() as s:
        await require_project(s, project_id, writable=True)
        memberships = list(
            await s.scalars(
                select(ProjectAsset).where(
                    ProjectAsset.project_id == project_id, ProjectAsset.asset_id.in_(body.asset_ids)
                )
            )
        )
        found = {m.asset_id for m in memberships}
        if found != set(body.asset_ids):
            raise AppError("asset_not_found", "部分素材不属于当前项目。", 404)
        for membership in memberships:
            membership.curated_at = None
        await s.execute(
            delete(ProjectAssetTag).where(
                ProjectAssetTag.project_id == project_id, ProjectAssetTag.asset_id.in_(body.asset_ids)
            )
        )
        for asset_id in body.asset_ids:
            asset = await s.get(Asset, asset_id)
            if asset:
                await state.storage.index(s, project_id, asset)
        record_event(
            s,
            "library.updated",
            project_id,
            body.asset_ids[0],
            action="key.remove",
            asset_ids=body.asset_ids,
        )
        return {"asset_ids": body.asset_ids}


@router.get("/assets/{asset_id}")
async def asset_detail(request: Request, asset_id: str):
    state = svc(request)
    async with state.db.session() as s:
        a = await s.get(Asset, asset_id)
        if not a:
            raise AppError("asset_not_found", "素材不存在。", 404)
        result = await state.storage.asset_view(s, a)
        projects = []
        for m in (
            await s.scalars(select(ProjectAsset).where(ProjectAsset.asset_id == asset_id))
        ).all():
            view = row_dict(m)
            view["tags"] = (await state.storage.tags_for(s, m.project_id, [asset_id])).get(asset_id, [])
            view["curated"] = bool(m.curated_at)
            projects.append(view)
        result["projects"] = projects
        if a.execution_id:
            e = await s.get(Execution, a.execution_id)
            result["execution"] = row_dict(e)
        return result


@router.head("/assets/{asset_id}/content")
@router.get("/assets/{asset_id}/content")
async def asset_content(request: Request, asset_id: str, download: bool = False):
    async with svc(request).storage.file_lock:
        a, b, path = await svc(request).storage.blob_path(asset_id)
        inline = (
            not download
            and a.media_type in ("image", "video", "audio")
            and b.mime_type.startswith(("image/", "video/", "audio/"))
            and b.mime_type != "image/svg+xml"
        )
        return media_response(
            request,
            path,
            etag=file_etag(path, b.sha256),
            media_type=b.mime_type,
            filename=a.name,
            disposition="inline" if inline else "attachment",
        )


@router.head("/assets/{asset_id}/preview")
@router.get("/assets/{asset_id}/preview")
async def asset_preview(request: Request, asset_id: str):
    state = svc(request)
    # GC takes the same lock: a request cannot recreate a preview after purge.
    async with state.storage.file_lock:
        async with state.db.session() as s:
            a = await s.get(Asset, asset_id)
            blob = await s.get(Blob, a.blob_id) if a else None
            if not a or not a.preview_path or not blob or blob.presence == "purged" or a.presence == "purged":
                raise AppError("preview_missing", "预览不可用。", 404)
            path = state.storage.resolve("previews/" + a.preview_path)
            if not path.is_file():
                raise AppError("preview_missing", "预览已缺失。", 404)
            preview, mime, etag = await asyncio.to_thread(small_preview, path)
            return media_response(request, preview, etag=etag, media_type=mime)


@router.get("/assets/{asset_id}/lineage")
async def lineage(request: Request, asset_id: str, depth: int = Query(3, ge=1, le=6)):
    async with svc(request).db.session() as s:
        nodes, edges, seen = [], [], {asset_id}
        frontier = {asset_id}
        for _ in range(depth):
            if not frontier or len(seen) > 150:
                break
            rows = (
                await s.scalars(
                    select(AssetRelation).where(
                        AssetRelation.from_asset_id.in_(frontier) | AssetRelation.to_asset_id.in_(frontier)
                    )
                )
            ).all()
            nxt = set()
            for relation in rows:
                if relation.id not in {e["id"] for e in edges}:
                    edges.append(row_dict(relation))
                nxt.update({relation.from_asset_id, relation.to_asset_id} - seen)
            seen.update(nxt)
            frontier = nxt
        for a in (await s.scalars(select(Asset).where(Asset.id.in_(seen)))).all():
            nodes.append(await svc(request).storage.asset_view(s, a))
        return {"nodes": nodes, "edges": edges}


@router.get("/projects/{project_id}/messages")
async def messages(
    request: Request,
    project_id: str,
    limit: int = Query(100, ge=1, le=300),
    conversation_id: str | None = None,
    before: str | None = None,
):
    async with svc(request).db.transaction() as s:
        await require_project(s, project_id)
        conversation = await resolve_conversation(s, project_id, conversation_id)
        query = select(Message).where(Message.project_id == project_id, Message.conversation_id == conversation.id)
        if before:
            boundary = await s.get(Message, before)
            if not boundary or boundary.project_id != project_id or boundary.conversation_id != conversation.id:
                raise AppError("message_cursor", "消息分页位置不属于当前对话。")
            query = query.where(
                or_(
                    Message.created_at < boundary.created_at,
                    and_(Message.created_at == boundary.created_at, Message.id < boundary.id),
                )
            )
        rows = (await s.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit))).all()
        items = [row_dict(m) for m in reversed(rows)]
        if conversation.mode != "autonomous":
            return items

        # Submission messages already carry a task ID, including in older conversations.
        task_ids = {item["task_id"] for item in items if item["role"] == "system" and item["task_id"]}
        task_calls = {}
        if task_ids:
            task_calls = {
                task_id: (run_id, call_id)
                for task_id, run_id, call_id in await s.execute(
                    select(Task.id, Task.agent_run_id, Task.agent_call_id).where(
                        Task.id.in_(task_ids),
                        Task.project_id == project_id,
                        Task.conversation_id == conversation.id,
                        Task.agent_run_id.is_not(None),
                    )
                )
            }
        message_runs = []
        for item in items:
            task_run, call_id = task_calls.get(item["task_id"], (None, None))
            run_id = task_run or (item["details"] or {}).get("agent_run_id")
            message_runs.append((run_id if isinstance(run_id, str) else None, call_id))
        run_ids = {run_id for run_id, _ in message_runs if run_id}
        if not run_ids:
            return items
        activity = {
            run_id: {"total": total, "failed": failed}
            for run_id, total, failed in await s.execute(
                select(
                    CreativeAgentRun.id,
                    func.count(CreativeAgentCall.id),
                    func.count(CreativeAgentCall.id).filter(CreativeAgentCall.state == "failed"),
                )
                .outerjoin(CreativeAgentCall, CreativeAgentCall.run_id == CreativeAgentRun.id)
                .where(
                    CreativeAgentRun.id.in_(run_ids),
                    CreativeAgentRun.project_id == project_id,
                    CreativeAgentRun.conversation_id == conversation.id,
                )
                .group_by(CreativeAgentRun.id)
            )
        }
        for item, (run_id, call_id) in zip(items, message_runs, strict=True):
            if run_id in activity:
                item.update(agent_run_id=run_id, agent_activity=activity[run_id])
                if call_id and item["role"] == "system":
                    item["agent_call_id"] = call_id
        return items


@router.post("/projects/{project_id}/messages", status_code=202)
async def send_message(request: Request, project_id: str, body: MessageRequest):
    started = asyncio.get_running_loop().create_future()

    def on_start(result):
        if not started.done():
            started.set_result(result)

    async def run():
        try:
            result = await svc(request).planner.prepare(project_id, body, on_start=on_start)
            on_start(result)
        except Exception as exc:
            if not started.done():
                started.set_exception(exc)

    svc(request).worker.spawn(run())
    return await started


@router.get("/projects/{project_id}/plans")
async def plans(request: Request, project_id: str, conversation_id: str | None = None):
    async with svc(request).db.transaction() as s:
        await require_project(s, project_id)
        conversation = await resolve_conversation(s, project_id, conversation_id)
        latest = (
            select(Plan.id, func.max(Plan.revision).label("revision"))
            .where(Plan.project_id == project_id, Plan.conversation_id == conversation.id)
            .group_by(Plan.id)
            .subquery()
        )
        rows = (
            await s.scalars(
                select(Plan)
                .join(latest, (Plan.id == latest.c.id) & (Plan.revision == latest.c.revision))
                .order_by(Plan.created_at.desc())
                .limit(30)
            )
        ).all()
        return [{**row_dict(p), "tool_name": svc(request).registry.get(p.tool_id)["name"]} for p in rows]


@router.post("/projects/{project_id}/plans")
async def manual_plan(request: Request, project_id: str, body: ManualPlan):
    return await svc(request).plans.create(project_id, body)


@router.patch("/plans/{plan_id}")
async def edit_plan(request: Request, plan_id: str, body: PlanPatch):
    async with svc(request).db.session() as s:
        p = await s.get(Plan, (plan_id, body.revision))
        if not p:
            raise AppError("plan_not_found", "方案不存在。", 404)
        match = request.headers.get("if-match")
        if match and match.strip('"') not in (p.plan_hash, str(p.revision)):
            raise AppError("revision_conflict", "方案已变化，请重新读取最新版本。", 412)
        project_id = p.project_id
    return await svc(request).plans.create(
        project_id,
        ManualPlan(**body.model_dump(exclude={"revision", "accept_previous_context", "prompt_mode"})),
        plan_id=plan_id,
        expected_revision=body.revision,
        prompt_mode=body.prompt_mode,
    )


@router.post("/plans/{plan_id}/dismiss")
async def dismiss_plan(request: Request, plan_id: str, revision: int = Body(embed=True)):
    async with svc(request).db.transaction() as s:
        p = await s.get(Plan, (plan_id, revision))
        if not p or p.state != "draft":
            raise AppError("plan_changed", "方案已变化。", 409)
        p.state = "dismissed"
        record_event(s, "plan.created", p.project_id, p.id)
        return {"dismissed": True}


@router.post("/plans/{plan_id}/execute")
async def execute_plan(request: Request, plan_id: str, body: ExecutePlan):
    result = await svc(request).plans.execute(plan_id, body)
    svc(request).worker.wake.set()
    return result


@router.get("/tasks")
async def tasks(
    request: Request,
    project_id: str | None = None,
    agent_run_id: str | None = None,
    limit: int = Query(80, ge=1, le=200),
):
    async with svc(request).db.session() as s:
        query = select(Task).order_by(Task.queue_seq.desc()).limit(limit)
        if project_id:
            query = query.where(Task.project_id == project_id)
        if agent_run_id:
            query = query.where(Task.agent_run_id == agent_run_id)
        task_rows = list((await s.scalars(query)).all())
        execution_rows = list(
            await s.scalars(
                select(Execution)
                .where(Execution.task_id.in_([t.id for t in task_rows]))
                .order_by(Execution.candidate_index)
            )
        )
        task_media, execution_media, titles = await resolve_task_media(
            s,
            task_rows,
            execution_rows,
            getattr(svc(request), "storage", None),
            svc(request).registry,
        )
        result = []
        for t in task_rows:
            es = [e for e in execution_rows if e.task_id == t.id]
            result.append(
                {
                    **row_dict(t),
                    "title": titles.get(t.id, ""),
                    "media": task_media.get(t.id, {}),
                    "tool_name": "生成视频"
                    if any(e.configuration.get("step_count", 1) > 1 for e in es)
                    else execution_identity(es[0], svc(request).registry)["tool_name"]
                    if es
                    else "",
                    "executions": [
                        {
                            "timing": execution_timing(e),
                            "media": execution_media[e.id],
                            **execution_identity(e, svc(request).registry),
                            **{
                                k: v
                                for k, v in row_dict(e).items()
                                if k not in ("history", "submitted_payload", "executed_payload")
                            },
                        }
                        for e in es
                    ],
                }
            )
        return result


@router.get("/tasks/{task_id}")
async def task_detail(request: Request, task_id: str):
    async with svc(request).db.session() as s:
        t = await s.get(Task, task_id)
        if not t:
            raise AppError("task_not_found", "任务不存在。", 404)
        es = (
            await s.scalars(select(Execution).where(Execution.task_id == task_id).order_by(Execution.candidate_index))
        ).all()
        task_media, execution_media, titles = await resolve_task_media(
            s,
            [t],
            es,
            getattr(svc(request), "storage", None),
            svc(request).registry,
        )
        result = {**row_dict(t), "title": titles.get(t.id, ""), "media": task_media.get(t.id, {}), "executions": []}
        for e in es:
            records = (await s.scalars(select(Artifact).where(Artifact.execution_id == e.id))).all()
            result["executions"].append(
                {
                    **row_dict(e),
                    "timing": execution_timing(e),
                    "media": execution_media[e.id],
                    **execution_identity(e, svc(request).registry),
                    "artifacts": [row_dict(a) for a in records],
                }
            )
        return result


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str):
    state = svc(request)
    async with state.db.transaction() as s:
        t = await s.get(Task, task_id)
        if not t:
            raise AppError("task_not_found", "任务不存在。", 404)
        t.cancel_requested = True
        for e in (await s.scalars(select(Execution).where(Execution.task_id == task_id))).all():
            if e.execution_state in ("pending", "waiting_external", "blocked"):
                e.execution_state, e.finished_at = "cancelled", now()
        record_event(s, "task.updated", t.project_id, task_id)
    await state.worker.aggregate(task_id)
    state.worker.wake.set()
    return {"cancel_requested": True}


@router.post("/tasks/{task_id}/retry")
async def retry_task(request: Request, task_id: str):
    async with svc(request).db.session() as s:
        t = await s.get(Task, task_id)
        if not t or t.state in ACTIVE_TASKS:
            raise AppError("retry_not_ready", "任务尚未结束，不能重做。", 409)
        plan = await s.get(Plan, (t.plan_id, t.plan_revision))
        executions = list(
            await s.scalars(select(Execution).where(Execution.task_id == task_id).order_by(Execution.candidate_index))
        )
        e = executions[0]
        continuation = (plan.prompt_metadata or {}).get("continuation")
        if continuation and e.execution_state == "succeeded":
            if e.storage_state != "complete":
                raise AppError("dependency_storage", "请先补存已经生成的起始画面，再继续视频步骤。", 409)
            if any(item.execution_state in ("unknown", "interrupted") for item in executions):
                raise AppError("reconcile_first", "请先核实未确定的执行状态，再准备重试。", 409)
            e = executions[-1]
            refs = e.inputs
            if not refs:
                frame = await s.scalar(
                    select(Asset)
                    .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                    .where(
                        Asset.execution_id == executions[0].id,
                        Asset.role == "final",
                        Asset.media_type == "image",
                        ProjectAsset.project_id == t.project_id,
                        ProjectAsset.removed_at.is_(None),
                    )
                )
                if not frame:
                    raise AppError("dependency_output", "已保存的起始画面不可用，请检查素材。", 409)
                refs = [dict(slot="source_image", asset_id=frame.id)]
            tool_id, parameters = e.tool_id, e.parameters
            inputs = [dict(slot=ref["slot"], asset_id=ref["asset_id"]) for ref in refs]
            prompt_metadata = e.configuration.get("prompt_generation")
            count = 1
        else:
            tool_id, parameters, inputs = plan.tool_id, plan.parameters, plan.inputs
            prompt_metadata, count = plan.prompt_metadata, plan.candidate_count
    # Retry is prepared as a new immutable plan; it still requires explicit execution.
    result = await svc(request).plans.create(
        t.project_id,
        ManualPlan(
            tool_id=tool_id,
            inputs=inputs,
            parameters=parameters,
            candidate_count=count,
            change_summary="从未完成步骤重试，复用已完成素材，保留参数与种子。"
            if continuation
            else "重做此前任务，保留原参数与种子。",
            based_on_execution_id=e.id,
        ),
        origin="retry",
        conversation_id=t.conversation_id,
        prompt_metadata=prompt_metadata,
    )
    return result


@router.post("/tasks/{task_id}/best")
async def best_asset(request: Request, task_id: str, asset_id: str = Body(embed=True)):
    async with svc(request).db.transaction() as s:
        t = await s.get(Task, task_id)
        a = await s.get(Asset, asset_id)
        e = await s.get(Execution, a.execution_id) if a and a.execution_id else None
        if not t or not e or e.task_id != task_id or a.role != "final" or a.source_info.get("internal_preparation"):
            raise AppError("candidate_invalid", "最佳候选必须是本任务的最终结果。")
        t.best_asset_id = asset_id
        record_event(s, "task.updated", t.project_id, t.id)
        return {"best_asset_id": asset_id}


@router.post("/executions/{execution_id}/collect")
async def collect_output(request: Request, execution_id: str):
    async with svc(request).db.session() as s:
        e = await s.get(Execution, execution_id)
        if not e:
            raise AppError("execution_not_found", "执行不存在。", 404)
        if e.execution_state not in TERMINAL:
            raise AppError("execution_active", "当前执行仍在核实或运行，完成后再补存。", 409)
    try:
        history = await svc(request).comfy.history(e.prompt_id)
    except httpx.HTTPError:
        history = None
    if history:
        await svc(request).worker.complete_from_history(execution_id, history)
    else:
        await svc(request).worker.collect(execution_id)
    return await task_detail(request, e.task_id)


@router.post("/executions/{execution_id}/reconcile")
async def reconcile_execution(request: Request, execution_id: str):
    async with svc(request).db.session() as s:
        e = await s.get(Execution, execution_id)
        if not e:
            raise AppError("execution_not_found", "执行不存在。", 404)
        if e.execution_state not in ("unknown", "interrupted"):
            raise AppError("reconcile_state", "该执行无需手动核实。", 409)
    await svc(request).worker.update(execution_id, execution_state="submission_unknown", finished_at=None)
    svc(request).worker.wake.set()
    return {"state": "submission_unknown"}


@router.get("/settings")
async def settings(request: Request):
    state = svc(request)
    public = state.settings.model_dump(mode="json")
    public.update(
        api_key_configured=bool(await state.planner.key()),
        api_key_environment=state.planner.key_environment,
        api_key_from_environment=bool(os.environ.get(state.planner.key_environment, "").strip()),
        usage=await state.planner.usage(),
    )
    async with state.db.session() as s:
        stored = await s.get(Setting, "runtime")
        public["revision"] = stored.value.get("revision", 1) if stored else 1
    return public


@router.put("/settings/credentials")
async def set_credentials(request: Request, api_key: str = Body(embed=True)):
    if len(api_key) > 512:
        raise AppError("credential_length", "密钥长度不正确。")
    await svc(request).planner.credentials(api_key)
    return {"configured": bool(await svc(request).planner.key())}


@router.patch("/settings")
async def edit_settings(request: Request, values: dict = Body(...)):
    state = svc(request)
    allowed = {
        "daily_budget_usd",
        "turn_budget_usd",
        "auto_free_after_idle_seconds",
        "image_timeout_seconds",
        "video_timeout_seconds",
        "upscale_max_edge",
        "max_outstanding_tasks",
    }
    if set(values) - allowed - {"revision"}:
        raise AppError("setting_unknown", "设置包含不支持的字段。")
    bounds = {
        "daily_budget_usd": (0.01, 100),
        "turn_budget_usd": (0.001, 10),
        "auto_free_after_idle_seconds": (0, 86400),
        "image_timeout_seconds": (60, 7200),
        "video_timeout_seconds": (60, 14400),
        "upscale_max_edge": (512, 8192),
        "max_outstanding_tasks": (1, 50),
    }
    async with state.db.transaction() as s:
        stored = await s.get(Setting, "runtime")
        previous = stored.value if stored else {"revision": 1}
        if values.get("revision") != previous["revision"]:
            raise AppError("revision_conflict", "设置已经更新，请刷新。", 409)
        updated = dict(previous)
        for name, value in values.items():
            if name == "revision":
                continue
            lo, hi = bounds[name]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= value <= hi:
                raise AppError("setting_range", f"{name}超出设置范围。")
            if name not in ("daily_budget_usd", "turn_budget_usd"):
                value = int(value)
            updated[name] = value
        updated["revision"] += 1
        if stored:
            stored.value = updated
        else:
            s.add(Setting(key="runtime", value=updated))
        for k, v in updated.items():
            if k != "revision":
                setattr(state.settings, k, v)
    return await settings(request)


@router.get("/diagnostics")
async def diagnostics(request: Request):
    state = svc(request)
    await state.worker.health(force=True)
    async with state.db.session() as s:
        db_status = (await s.execute(text("PRAGMA quick_check"))).scalar()
        attention = await s.scalar(select(func.count()).select_from(Task).where(Task.state == "needs_attention"))
    return {
        "application": {
            "version": "0.1.0",
            "data_dir": str(state.settings.data_dir),
            "database": db_status,
            "needs_attention": attention,
        },
        "comfyui": state.worker.status,
        "devices": (state.worker.stats or {}).get("devices", []),
        "tools": state.registry.public(),
        "usage": await state.planner.usage(),
    }


@router.get("/events")
async def events(request: Request, after: int = 0, project_id: str | None = None, task_id: str | None = None):
    cursor = max(after, int(request.headers.get("last-event-id", "0") or "0"))

    async def stream():
        nonlocal cursor
        async with svc(request).db.session() as s:
            first = await s.scalar(select(func.min(Event.id)))
            last = await s.scalar(select(func.max(Event.id))) or 0
            if first and cursor < first - 1:
                cursor = last
                yield f'id: {cursor}\ndata: {{"type":"reset"}}\n\n'
        heartbeat = asyncio.get_running_loop().time()
        while not await request.is_disconnected() and not getattr(svc(request), "shutdown_requested", False):
            async with svc(request).db.session() as s:
                query = select(Event).where(Event.id > cursor)
                if project_id:
                    query = query.where(Event.project_id == project_id)
                if task_id:
                    ids = select(Execution.id).where(Execution.task_id == task_id)
                    asset_ids = select(Asset.id).where(Asset.execution_id.in_(ids))
                    query = query.where(
                        (Event.entity_id == task_id) | Event.entity_id.in_(ids) | Event.entity_id.in_(asset_ids)
                    )
                rows = (await s.scalars(query.order_by(Event.id).limit(100))).all()
            for event in rows:
                cursor = event.id
                yield f"id: {event.id}\ndata: {json.dumps(row_dict(event), ensure_ascii=False)}\n\n"
            if not rows and asyncio.get_running_loop().time() - heartbeat >= 15:
                yield ": heartbeat\n\n"
                heartbeat = asyncio.get_running_loop().time()
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.post("/gc/preview")
async def gc_preview(request: Request):
    return await svc(request).maintenance.preview_gc()


@router.post("/gc/{job_id}/commit")
async def gc_commit(request: Request, job_id: str, preview_hash: str = Body(embed=True)):
    return await svc(request).maintenance.gc(job_id, preview_hash)


@router.post("/backups")
async def backup(request: Request):
    return await svc(request).maintenance.backup()


@router.get("/backups")
async def backups(request: Request):
    async with svc(request).db.session() as s:
        return [
            row_dict(b)
            for b in (
                await s.scalars(
                    select(Maintenance).where(Maintenance.kind == "backup").order_by(Maintenance.created_at.desc())
                )
            ).all()
        ]


@router.get("/backups/{backup_id}")
async def download_backup(request: Request, backup_id: str):
    async with svc(request).db.session() as s:
        b = await s.get(Maintenance, backup_id)
        if not b or b.kind != "backup":
            raise AppError("backup_not_found", "备份不存在。", 404)
        path = svc(request).storage.resolve("backups/" + b.data["filename"])
        return FileResponse(path, filename=b.data["filename"], media_type="application/zip")


@router.post("/backups/restore")
async def restore_backup(request: Request, file: UploadFile = File(...)):
    staged = await svc(request).storage.stage_upload(file)
    try:
        return await svc(request).maintenance.restore(staged)
    finally:
        await file.close()
