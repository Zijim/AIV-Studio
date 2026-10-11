import asyncio
import json
import os
import shutil

import httpx
from fastapi import APIRouter, Body, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import String, func, select, text

from .config import ROOT
from .errors import AppError
from .models import (
    Artifact,
    Asset,
    AssetRelation,
    Event,
    Execution,
    ImportBatch,
    Maintenance,
    Message,
    Plan,
    Project,
    ProjectAsset,
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
    ExecutePlan,
    ManualPlan,
    MessageRequest,
    PlanPatch,
    ProjectCreate,
    ProjectUpdate,
)
from .worker import TERMINAL

router = APIRouter(prefix="/api/v1")


def svc(request):
    return request.app.state


@router.get("/session")
async def session(request: Request):
    from fastapi.responses import JSONResponse

    async with svc(request).db.session() as s:
        cursor = await s.scalar(select(func.max(Event.id))) or 0
    response = JSONResponse({"csrf_token": svc(request).csrf, "version": "0.1.0", "event_cursor": cursor})
    response.set_cookie("aiv_session", svc(request).session_token, httponly=True, samesite="strict")
    return response


@router.get("/health")
async def health(request: Request):
    state = svc(request)
    return {
        "status": "ok",
        "version": "0.1.0",
        "workspace_root": str(ROOT),
        "data_dir": str(state.settings.data_dir),
        "process_id": os.getpid(),
        "comfyui": state.worker.status,
        "planner": {"configured": bool(await state.planner.key()), "model": state.settings.llm_model},
        "storage": {
            "free_bytes": shutil.disk_usage(state.settings.data_dir).free,
            "minimum_free_bytes": state.settings.minimum_free_bytes,
        },
    }


@router.get("/tools")
async def tools(request: Request):
    return svc(request).registry.public()


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
    removed: bool = False,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cursor: str | None = None,
):
    if cursor is not None:
        if not cursor.isdecimal() or len(cursor) > 10:
            raise AppError("cursor_invalid", "分页位置无效。")
        offset = int(cursor)
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
        rows = (await s.execute(query.order_by(Asset.created_at.desc()).offset(offset).limit(limit))).all()
        return {
            "items": [await state.storage.asset_view(s, a, m) for a, m in rows],
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
                {"name": upload.filename, "state": "failed", "error": getattr(exc, "message", type(exc).__name__)}
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
            membership.removed_at = None
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
            raise AppError("revision_conflict", "素材备注已变化，请刷新。", 409)
        m.note, m.usage, m.tags = body.note, body.usage, body.tags
        m.revision += 1
        p.revision += 1
        await svc(request).storage.index(s, project_id, await s.get(Asset, asset_id))
        record_event(s, "asset.updated", project_id, asset_id)
        return row_dict(m)


@router.delete("/projects/{project_id}/assets/{asset_id}")
async def unlink_asset(request: Request, project_id: str, asset_id: str):
    async with svc(request).db.transaction() as s:
        await require_project(s, project_id, writable=True)
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


@router.get("/assets/{asset_id}")
async def asset_detail(request: Request, asset_id: str):
    async with svc(request).db.session() as s:
        a = await s.get(Asset, asset_id)
        if not a:
            raise AppError("asset_not_found", "素材不存在。", 404)
        result = await svc(request).storage.asset_view(s, a)
        result["projects"] = [
            row_dict(m) for m in (await s.scalars(select(ProjectAsset).where(ProjectAsset.asset_id == asset_id))).all()
        ]
        if a.execution_id:
            e = await s.get(Execution, a.execution_id)
            result["execution"] = row_dict(e)
        return result


@router.head("/assets/{asset_id}/content")
@router.get("/assets/{asset_id}/content")
async def asset_content(request: Request, asset_id: str, download: bool = False):
    a, b, path = await svc(request).storage.blob_path(asset_id)
    inline = (
        not download
        and a.media_type in ("image", "video", "audio")
        and b.mime_type.startswith(("image/", "video/", "audio/"))
        and b.mime_type != "image/svg+xml"
    )
    return FileResponse(
        path,
        media_type=b.mime_type,
        filename=a.name,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
    )


@router.head("/assets/{asset_id}/preview")
@router.get("/assets/{asset_id}/preview")
async def asset_preview(request: Request, asset_id: str):
    async with svc(request).db.session() as s:
        a = await s.get(Asset, asset_id)
        if not a or not a.preview_path:
            raise AppError("preview_missing", "预览不可用。", 404)
        path = svc(request).storage.resolve("previews/" + a.preview_path)
        if not path.exists():
            raise AppError("preview_missing", "预览已缺失。", 404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


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
async def messages(request: Request, project_id: str, limit: int = Query(100, ge=1, le=300)):
    async with svc(request).db.session() as s:
        await require_project(s, project_id)
        rows = (
            await s.scalars(
                select(Message).where(Message.project_id == project_id).order_by(Message.created_at.desc()).limit(limit)
            )
        ).all()
        return [row_dict(m) for m in reversed(rows)]


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
async def plans(request: Request, project_id: str):
    async with svc(request).db.session() as s:
        await require_project(s, project_id)
        latest = (
            select(Plan.id, func.max(Plan.revision).label("revision"))
            .where(Plan.project_id == project_id)
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
        ManualPlan(**body.model_dump(exclude={"revision", "accept_previous_context"})),
        plan_id=plan_id,
        expected_revision=body.revision,
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
async def tasks(request: Request, project_id: str | None = None, limit: int = Query(80, ge=1, le=200)):
    async with svc(request).db.session() as s:
        query = select(Task).order_by(Task.queue_seq.desc()).limit(limit)
        if project_id:
            query = query.where(Task.project_id == project_id)
        result = []
        for t in (await s.scalars(query)).all():
            es = (
                await s.scalars(select(Execution).where(Execution.task_id == t.id).order_by(Execution.candidate_index))
            ).all()
            result.append(
                {
                    **row_dict(t),
                    "tool_name": svc(request).registry.get(es[0].tool_id)["name"] if es else "",
                    "executions": [
                        {
                            k: v
                            for k, v in row_dict(e).items()
                            if k not in ("history", "submitted_payload", "executed_payload")
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
        result = {**row_dict(t), "executions": []}
        for e in es:
            records = (await s.scalars(select(Artifact).where(Artifact.execution_id == e.id))).all()
            result["executions"].append({**row_dict(e), "artifacts": [row_dict(a) for a in records]})
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
            if e.execution_state in ("pending", "waiting_external"):
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
        e = await s.scalar(
            select(Execution).where(Execution.task_id == task_id).order_by(Execution.candidate_index).limit(1)
        )
    # Retry is prepared as a new immutable plan; it still requires explicit execution.
    result = await svc(request).plans.create(
        t.project_id,
        ManualPlan(
            tool_id=plan.tool_id,
            inputs=plan.inputs,
            parameters=plan.parameters,
            candidate_count=plan.candidate_count,
            change_summary="重做此前任务，保留原参数与种子。",
            based_on_execution_id=e.id,
        ),
        origin="retry",
    )
    return result


@router.post("/tasks/{task_id}/best")
async def best_asset(request: Request, task_id: str, asset_id: str = Body(embed=True)):
    async with svc(request).db.transaction() as s:
        t = await s.get(Task, task_id)
        a = await s.get(Asset, asset_id)
        e = await s.get(Execution, a.execution_id) if a and a.execution_id else None
        if not t or not e or e.task_id != task_id or a.role != "final":
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
        api_key_from_environment=bool(__import__("os").environ.get("DEEPSEEK_API_KEY")),
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
