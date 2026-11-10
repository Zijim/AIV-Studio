"""Opt-in live smoke check in an isolated workspace, using configured credentials.

python scripts/validate_creative_agent.py [--generate]
Default: short document. --generate: one 512px image, only when ComfyUI is idle.
Uses real model calls. Never reads the user's project database or materials.
"""

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.comfy import ComfyClient
from app.config import ROOT, load_settings
from app.creative_agent import CreativeAgent
from app.creative_agent_schema import StartRun
from app.db import Database
from app.models import (
    Base,
    Conversation,
    CreativeAgentCall,
    CreativeAgentRun,
    Project,
    uid,
)
from app.planner import Planner
from app.plans import PlanService
from app.registry import Registry
from app.storage import Storage, atomic_json
from app.worker import Worker
from sqlalchemy import select, text


async def main(generate):
    settings = load_settings()
    root = ROOT / ".data" / "agent-validation" / uid()
    settings = settings.model_copy(
        update={
            "data_dir": root,
            "agent_max_executions": 1,
            "agent_max_model_requests": 12,
            "agent_max_tool_calls": 30,
            "agent_active_seconds": 600,
            "auto_free_after_idle_seconds": 0,
        }
    )
    for name in (
        "staging",
        "blobs",
        "previews",
        "registry",
        "executions",
        "backups",
        "logs",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    comfy = ComfyClient(settings.comfy_url, settings.comfy_output_root)
    db = Database(root / "app.sqlite3")
    worker_job = agent_job = None
    try:
        if generate:
            queue = await comfy.queue()
            if queue.get("queue_running") or queue.get("queue_pending"):
                raise RuntimeError("ComfyUI is busy; live generation check skipped.")
        async with db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text(
                    "CREATE VIRTUAL TABLE asset_search USING fts5(entry_id UNINDEXED, project_id UNINDEXED, asset_id UNINDEXED, body)"
                )
            )
        registry = Registry(ROOT / "tool_registry", root)
        storage = Storage(db, settings)
        plans = PlanService(db, registry, storage, settings)
        planner = Planner(db, settings, registry, plans, storage)
        worker = Worker(db, comfy, registry, storage, settings)
        await worker.health(force=True)
        state = SimpleNamespace(
            db=db,
            settings=settings,
            registry=registry,
            storage=storage,
            planner=planner,
            plans=plans,
            worker=worker,
        )
        agent = CreativeAgent(state)
        async with db.transaction() as session:
            project = Project(name="独立 Agent 闭环验证")
            session.add(project)
            await session.flush()
            conversation = Conversation(project_id=project.id, mode="autonomous")
            session.add(conversation)
            await session.flush()
        goal = (
            "生成一张512×512的极简插画：纯白背景中一只橙色纸船，不要文字。只生成一个候选；检查实际画面后交付图片。"
            if generate
            else "请写一份约100字的中文纸船插画创意说明，保存为Markdown文档，检查全文后直接交付，不需要生成图片。"
        )
        result = await agent.start_run(
            project.id,
            StartRun(
                conversation_id=conversation.id,
                content=goal,
                use_key_assets=False,
                idempotency_key=uid(),
            ),
        )
        if generate:
            worker_job = asyncio.create_task(worker.run())
        agent_job = asyncio.create_task(agent.serve())
        previous = None
        for _ in range(600):
            async with db.session() as session:
                run = await session.get(CreativeAgentRun, result["run_id"])
                calls = (
                    await session.scalars(
                        select(CreativeAgentCall)
                        .where(CreativeAgentCall.run_id == run.id)
                        .order_by(CreativeAgentCall.created_at)
                    )
                ).all()
            snapshot = (run.state, len(calls), run.model_count, run.execution_count)
            if snapshot != previous:
                print(
                    json.dumps(
                        {
                            "state": run.state,
                            "calls": len(calls),
                            "model_requests": run.model_count,
                            "executions": run.execution_count,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                previous = snapshot
            if run.state in (
                "completed",
                "failed",
                "cancelled",
                "needs_attention",
                "waiting_user",
            ):
                report = {
                    "run_id": run.id,
                    "state": run.state,
                    "kind": (run.completion or {}).get("kind"),
                    "model": settings.llm_model,
                    "execution_count": run.execution_count,
                    "deliverables": run.deliverables,
                    "stop_reason": run.stop_reason,
                    "completion": run.completion,
                    "tools": [
                        {
                            "name": c.name,
                            "state": c.state,
                            "error": (c.result or {}).get("error"),
                        }
                        for c in calls
                    ],
                }
                atomic_json(root / "report.json", report)
                print(
                    json.dumps(
                        {
                            "report": str(root / "report.json"),
                            "state": run.state,
                            "stop_reason": run.stop_reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return (
                    0
                    if run.state == "completed" and report["kind"] == "delivered"
                    else 1
                )
            await asyncio.sleep(1)
        return 2
    finally:
        if "agent" in locals():
            await agent.stop()
        if "worker" in locals():
            await worker.stop()
        for job in (agent_job, worker_job):
            if job:
                job.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await job
        await comfy.close()
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.generate)))
