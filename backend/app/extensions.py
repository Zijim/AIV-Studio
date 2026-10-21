import asyncio
import ctypes
import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from . import api
from .conversations import resolve_conversation
from .errors import AppError
from .models import AgentTurn, LLMCall, Plan, record_event, row_dict
from .plans import require_project
from .schemas import PARAMETER_MODELS, MessageRequest

router = APIRouter(prefix="/api/v1")


@router.get("/capabilities")
async def capabilities(request: Request):
    return {
        "health": await api.health(request),
        "tools": request.app.state.registry.public(),
        "planner": {
            "provider": "deepseek_official_responses",
            "structured_output": True,
            "max_calls_per_turn": 2,
            "vision": True,
            "multiple_conversations": True,
        },
    }


@router.get("/tools/{tool_id}")
async def tool(request: Request, tool_id: str):
    value = next((t for t in request.app.state.registry.public() if t["id"] == tool_id), None)
    if not value:
        raise AppError("tool_not_found", "工具不存在。", 404)
    return {**value, "parameter_schema": PARAMETER_MODELS[tool_id].model_json_schema()}


@router.get("/projects/{project_id}/turns")
async def turns(request: Request, project_id: str, conversation_id: str | None = None):
    async with request.app.state.db.transaction() as s:
        await require_project(s, project_id)
        conversation = await resolve_conversation(s, project_id, conversation_id)
        return [
            row_dict(t)
            for t in (
                await s.scalars(
                    select(AgentTurn)
                    .where(AgentTurn.project_id == project_id, AgentTurn.conversation_id == conversation.id)
                    .order_by(AgentTurn.created_at.desc())
                    .limit(30)
                )
            ).all()
        ]


@router.get("/turns/{turn_id}")
async def turn(request: Request, turn_id: str):
    async with request.app.state.db.session() as s:
        t = await s.get(AgentTurn, turn_id)
        if not t:
            raise AppError("turn_not_found", "方案回合不存在。", 404)
        return row_dict(t)


@router.post("/turns/{turn_id}/clarifications", status_code=202)
async def clarify(request: Request, turn_id: str, body: MessageRequest):
    t = await turn(request, turn_id)
    if body.conversation_id and body.conversation_id != t["conversation_id"]:
        raise AppError("conversation_mismatch", "澄清必须留在原来的对话中。", 409)
    body = body.model_copy(update={"conversation_id": t["conversation_id"]})
    return await api.send_message(request, t["project_id"], body)


@router.post("/turns/{turn_id}/abandon")
async def abandon(request: Request, turn_id: str):
    state = request.app.state
    async with state.db.transaction() as s:
        t = await s.get(AgentTurn, turn_id)
        if not t:
            raise AppError("turn_not_found", "方案回合不存在。", 404)
        if t.state == "executing":
            raise AppError("turn_executing", "方案已经执行，请通过任务取消操作。", 409)
        t.state = "abandoned"
        for p in (await s.scalars(select(Plan).where(Plan.turn_id == turn_id, Plan.state == "draft"))).all():
            p.state = "dismissed"
        record_event(s, "turn.updated", t.project_id, t.id)
        record_event(s, "plan.created", t.project_id, t.id)
    if task := state.planner.inflight.get(turn_id):
        task.cancel()
    return {"state": "abandoned"}


@router.get("/plans/{plan_id}")
async def get_plan(request: Request, plan_id: str, revision: int | None = None):
    async with request.app.state.db.session() as s:
        query = select(Plan).where(Plan.id == plan_id)
        if revision:
            query = query.where(Plan.revision == revision)
        p = await s.scalar(query.order_by(Plan.revision.desc()).limit(1))
        if not p:
            raise AppError("plan_not_found", "方案不存在。", 404)
        return row_dict(p)


router.add_api_route("/projects/{project_id}/manual-plans", api.manual_plan, methods=["POST"])


@router.post("/projects/{project_id}/unarchive")
async def unarchive(request: Request, project_id: str):
    return await api.archive_project(request, project_id, False)


@router.post("/projects/{project_id}/assets/attach")
async def attach(request: Request, project_id: str, asset_id: str = Body(embed=True)):
    return await api.attach_asset(request, project_id, asset_id)


@router.post("/tasks/{task_id}/collect")
async def collect_task(request: Request, task_id: str):
    detail = await api.task_detail(request, task_id)
    for e in detail["executions"]:
        await api.collect_output(request, e["id"])
    return await api.task_detail(request, task_id)


@router.put("/candidate-groups/{task_id}/best")
async def candidate_best(request: Request, task_id: str, asset_id: str | None = Body(embed=True)):
    if asset_id:
        return await api.best_asset(request, task_id, asset_id)
    from .models import Task

    async with request.app.state.db.transaction() as s:
        t = await s.get(Task, task_id)
        if not t:
            raise AppError("task_not_found", "任务不存在。", 404)
        t.best_asset_id = None
        record_event(s, "task.updated", t.project_id, t.id)
    return {"best_asset_id": None}


@router.get("/search")
async def search(
    request: Request,
    q: str,
    project_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await api.assets(request, project_id, q, None, False, limit, offset)


@router.post("/imports")
async def import_alias(request: Request, project_id: str = Form(...), files: list[UploadFile] = File(...)):
    return await api.import_files(request, project_id, files)


router.add_api_route("/cleanup/preview", api.gc_preview, methods=["POST"])


@router.post("/cleanup/execute")
async def cleanup(request: Request, preview_id: str = Body(), preview_hash: str = Body()):
    return await api.gc_commit(request, preview_id, preview_hash)


router.add_api_route("/settings/llm-key", api.set_credentials, methods=["PUT"])


@router.delete("/settings/llm-key")
async def delete_key(request: Request):
    return await api.set_credentials(request, "")


@router.get("/usage")
async def usage(request: Request):
    state = request.app.state
    result = await state.planner.usage()
    async with state.db.session() as s:
        calls = (await s.scalars(select(LLMCall).order_by(LLMCall.created_at.desc()).limit(100))).all()
        result["calls"] = [
            {k: v for k, v in row_dict(c).items() if k not in ("request_data", "response_data")} for c in calls
        ]
    return result


@router.get("/resources")
async def resources(request: Request):
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong)
            for n in (
                "total_physical",
                "available_physical",
                "total_page",
                "available_page",
                "total_virtual",
                "available_virtual",
                "available_extended",
            )
        ]

    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
    state = request.app.state
    health = await api.health(request)
    return {
        "ram": {
            "total_bytes": memory.total_physical,
            "available_bytes": memory.available_physical,
            "load_percent": memory.load,
        },
        "storage": health["storage"],
        "devices": (state.worker.stats or {}).get("devices", []),
        "comfyui": state.worker.status,
    }


def output_path(state, relative):
    root = Path(state.settings.comfy_output_root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise AppError("output_path", "文件不存在或不在配置的ComfyUI输出目录内。", 404)
    return root, path


@router.get("/comfy-output-files")
async def output_files(
    request: Request, q: str = "", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
):
    state = request.app.state
    root = Path(state.settings.comfy_output_root).resolve()
    if not root.is_dir():
        raise AppError("output_root", "配置的ComfyUI输出目录不存在，请修改config.toml。", 409)

    def scan():
        entries = []
        for path in root.rglob("*"):
            if path.is_file() and q.casefold() in path.name.casefold():
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                entries.append(
                    {
                        "path": relative,
                        "name": path.name,
                        "byte_size": stat.st_size,
                        "modified_at": stat.st_mtime,
                        "mime_type": mime,
                        "preview_url": "/api/v1/comfy-output-files/content?path=" + quote(relative, safe="")
                        if mime.startswith("image/")
                        else None,
                    }
                )
        entries.sort(key=lambda r: (r["modified_at"], r["path"]), reverse=True)
        return {
            "items": entries[offset : offset + limit],
            "total": len(entries),
            "next_cursor": str(offset + limit) if offset + limit < len(entries) else None,
        }

    return await asyncio.to_thread(scan)


@router.get("/comfy-output-files/content")
async def output_content(request: Request, path: str):
    _, source = output_path(request.app.state, path)
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    inline = mime.startswith(("image/", "video/", "audio/")) and mime != "image/svg+xml"
    return FileResponse(
        source, media_type=mime, filename=source.name, content_disposition_type="inline" if inline else "attachment"
    )


@router.post("/imports/comfy-output")
async def import_outputs(request: Request, project_id: str = Body(), paths: list[str] = Body()):
    if not paths or len(paths) > 50:
        raise AppError("import_count", "请选择1到50个文件。")
    uploads = []
    try:
        for relative in paths:
            root, source = output_path(request.app.state, relative)
            if source.stat().st_size > request.app.state.settings.max_import_file_bytes:
                raise AppError("file_size", "文件超过导入上限。")
            uploads.append(UploadFile(file=source.open("rb"), filename=source.relative_to(root).as_posix()))
        return await api.import_files(request, project_id, uploads)
    finally:
        for upload in uploads:
            await upload.close()
