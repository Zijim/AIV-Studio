import json

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from .creative_agent_schema import Answer, RunControl, StartRun
from .creative_agent_scope import TERMINAL_RUNS, scope_summary
from .errors import AppError
from .models import Asset, CreativeAgentCall, CreativeAgentRun, row_dict
from .plans import require_project

router = APIRouter(prefix="/api/v1")


async def run_extras(session, run):
    """Human-facing context: allow-listed material summary and saved documents."""
    documents = (
        await session.execute(
            select(CreativeAgentCall, Asset)
            .join(Asset, Asset.id == CreativeAgentCall.asset_id)
            .where(
                CreativeAgentCall.run_id == run.id,
                CreativeAgentCall.asset_id.is_not(None),
                CreativeAgentCall.state == "completed",
            )
            .order_by(CreativeAgentCall.created_at)
        )
    ).all()
    return {
        "scope_summary": await scope_summary(session, run),
        "documents": [
            {
                "asset_id": asset.id,
                "blob_id": asset.blob_id,
                "name": asset.name,
                "media_type": asset.media_type,
                "parent_asset_id": (asset.source_info or {}).get("parent_asset_id"),
                "created_at": asset.created_at,
            }
            for _call, asset in documents
        ],
    }


@router.post("/projects/{project_id}/agent-runs", status_code=202)
async def start_run(request: Request, project_id: str, body: StartRun):
    return await request.app.state.creative_agent.start_run(project_id, body, public_access=request.state.public_access)


@router.get("/projects/{project_id}/agent-runs")
async def list_runs(
    request: Request, project_id: str, conversation_id: str | None = None, limit: int = Query(30, ge=1, le=100)
):
    async with request.app.state.db.session() as session:
        await require_project(session, project_id)
        query = select(CreativeAgentRun).where(CreativeAgentRun.project_id == project_id)
        if conversation_id:
            query = query.where(CreativeAgentRun.conversation_id == conversation_id)
        rows = (await session.scalars(query.order_by(CreativeAgentRun.created_at.desc()).limit(limit))).all()
        # The latest run and any active run need the panel extras. Historical
        # terminal runs get them from GET /agent-runs/{id} only when opened.
        detailed = {row.id for row in rows[:1]} | {row.id for row in rows if row.state not in TERMINAL_RUNS}
        items = []
        for row in rows:
            item = row_dict(row)
            if row.id in detailed:
                item.update(await run_extras(session, row))
            items.append(item)
        return {"items": items, "terminal_states": TERMINAL_RUNS}


@router.get("/agent-runs/{run_id}")
async def get_run(request: Request, run_id: str):
    async with request.app.state.db.session() as session:
        run = await session.get(CreativeAgentRun, run_id)
        if not run:
            raise AppError("run_not_found", "自主创作记录不存在。", 404)
        data = row_dict(run)
        data.update(await run_extras(session, run))
        return data


@router.get("/agent-runs/{run_id}/activity")
async def activity(request: Request, run_id: str):
    async with request.app.state.db.session() as session:
        if not await session.get(CreativeAgentRun, run_id):
            raise AppError("run_not_found", "自主创作记录不存在。", 404)
        calls = (
            await session.scalars(
                select(CreativeAgentCall)
                .where(CreativeAgentCall.run_id == run_id)
                .order_by(CreativeAgentCall.created_at, CreativeAgentCall.position)
            )
        ).all()
        # UI audit never exposes hidden model reasoning or credential/request data.
        return {
            "items": [
                {
                    "id": c.id,
                    "call_id": c.call_id,
                    "name": c.name,
                    "state": c.state,
                    "result": c.result,
                    "created_at": c.created_at,
                    "question": json.loads(c.arguments) if c.name == "ask_user" and c.state == "waiting" else None,
                }
                for c in calls
            ]
        }


@router.get("/agent-runs/{run_id}/calls/{call_id}")
async def call_detail(request: Request, run_id: str, call_id: str):
    async with request.app.state.db.session() as session:
        if not await session.get(CreativeAgentRun, run_id):
            raise AppError("run_not_found", "自主创作记录不存在。", 404)
        call = await session.scalar(
            select(CreativeAgentCall).where(
                CreativeAgentCall.id == call_id,
                CreativeAgentCall.run_id == run_id,
            )
        )
        if not call:
            raise AppError("call_not_found", "工具调用记录不存在。", 404)
        try:
            arguments = json.loads(call.arguments)
        except (TypeError, ValueError):
            arguments = call.arguments
        return {
            "id": call.id,
            "call_id": call.call_id,
            "name": call.name,
            "state": call.state,
            "created_at": call.created_at,
            "plan_id": call.plan_id,
            "arguments": arguments,
            "result": call.result,
        }


@router.post("/agent-runs/{run_id}/answers")
async def answer(request: Request, run_id: str, body: Answer):
    return await request.app.state.creative_agent.answer(run_id, body)


@router.post("/agent-runs/{run_id}/{action}")
async def control(request: Request, run_id: str, action: str, body: RunControl):
    if action not in ("pause", "resume", "cancel"):
        raise AppError("not_found", "接口不存在。", 404)
    return await request.app.state.creative_agent.control(run_id, action, body.control_version)
