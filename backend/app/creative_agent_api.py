import json

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from .creative_agent_schema import Answer, RunControl, StartRun
from .creative_agent_scope import TERMINAL_RUNS
from .errors import AppError
from .models import CreativeAgentCall, CreativeAgentRun, row_dict
from .plans import require_project

router = APIRouter(prefix="/api/v1")


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
        return {"items": [row_dict(r) for r in rows], "terminal_states": TERMINAL_RUNS}


@router.get("/agent-runs/{run_id}")
async def get_run(request: Request, run_id: str):
    async with request.app.state.db.session() as session:
        run = await session.get(CreativeAgentRun, run_id)
        if not run:
            raise AppError("run_not_found", "自主创作记录不存在。", 404)
        return row_dict(run)


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


@router.post("/agent-runs/{run_id}/answers")
async def answer(request: Request, run_id: str, body: Answer):
    return await request.app.state.creative_agent.answer(run_id, body)


@router.post("/agent-runs/{run_id}/{action}")
async def control(request: Request, run_id: str, action: str, body: RunControl):
    if action not in ("pause", "resume", "cancel"):
        raise AppError("not_found", "接口不存在。", 404)
    return await request.app.state.creative_agent.control(run_id, action, body.control_version)
