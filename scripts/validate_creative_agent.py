"""Opt-in live smoke check in an isolated workspace, using configured credentials.

python scripts/validate_creative_agent.py [--generate | --scope | --background]
Default: short document. --generate: one 512px image, only when ComfyUI is idle.
--scope: live material narrowing, with synthetic selected/key documents and no GPU.
--background: real model attempt blocked by a mid-run project goal change; no GPU.
--resume DIR: recover an existing isolated media run; never starts a new generation.
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
    CreativeAgentStep,
    Execution,
    Project,
    ProjectAsset,
    Task,
    uid,
)
from app.planner import Planner
from app.plans import PlanService
from app.registry import Registry
from app.storage import Storage, atomic_json
from app.worker import Worker
from sqlalchemy import select, text


async def main(generate, scope_check=False, resume=None, background_check=False):
    settings = load_settings()
    validation_root = (ROOT / ".data" / "agent-validation").resolve()
    root = Path(resume).resolve() if resume else validation_root / uid()
    if resume and (root.parent != validation_root or not (root / "app.sqlite3").is_file()):
        raise RuntimeError("Resume only accepts an existing direct child of .data/agent-validation.")
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
        if generate and not resume:
            queue = await comfy.queue()
            if queue.get("queue_running") or queue.get("queue_pending"):
                raise RuntimeError("ComfyUI is busy; live generation check skipped.")
        async with db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS asset_search USING fts5(entry_id UNINDEXED, project_id UNINDEXED, asset_id UNINDEXED, body)"
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
        if not resume:
            async with db.transaction() as session:
                project = Project(name="独立 Agent 闭环验证")
                session.add(project)
                await session.flush()
                conversation = Conversation(project_id=project.id, mode="autonomous")
                session.add(conversation)
                await session.flush()
        goal = (
            "生成一张512×512的纯白背景橙色纸船插画，检查实际画面后交付图片。"
            if background_check
            else "生成一张512×512的极简插画：纯白背景中一只橙色纸船，不要文字。只生成一个候选；检查实际画面后交付图片。"
            if generate
            else "请写一份约100字的中文纸船插画创意说明，保存为Markdown文档，检查全文后直接交付，不需要生成图片。"
        )
        selected = []
        forbidden = "SCOPE_KEY_MUST_NEVER_REACH_MODEL"
        scope_requests_safe = True
        if scope_check:
            seeded = []
            for name, content in (
                ("chosen.md", "明确指定的正文"),
                ("excluded.md", "明确排除的正文"),
                (forbidden + ".md", forbidden),
            ):
                path = root / "staging" / name
                path.write_text(content, encoding="utf-8")
                seeded.append(await storage.ingest(path, name, project.id))
            selected = seeded[:2]
            async with db.transaction() as session:
                (await session.get(ProjectAsset, (project.id, seeded[2]))).curated_at = "2026-11-10"
            goal = (
                "本次只用 chosen.md，排除 excluded.md，也不要读取项目关键素材。"
                "请列出本次实际可用素材的文件名；只回复讨论，不生成图片、视频或文档。"
            )

            async def checked_provider(request):
                nonlocal scope_requests_safe
                if forbidden in json.dumps(request, ensure_ascii=False):
                    scope_requests_safe = False
                    raise RuntimeError("Material scope leak detected before sending a model request.")
                return await agent.request_model(request)

            agent.provider = checked_provider
        if resume:
            async with db.session() as session:
                existing = await session.scalar(select(CreativeAgentRun).order_by(CreativeAgentRun.created_at.desc()))
                task = await session.scalar(select(Task).where(Task.agent_run_id == existing.id)) if existing else None
            if not existing or not task:
                raise RuntimeError("The isolated workspace has no existing media execution to recover.")
            result = {"run_id": existing.id}
            generate = True
            await worker.recover()
            await agent.recover()
        else:
            result = await agent.start_run(
                project.id,
                StartRun(
                    conversation_id=conversation.id,
                    content=goal,
                    use_key_assets=scope_check,
                    selected_asset_ids=selected,
                    idempotency_key=uid(),
                ),
            )
        background = {"changed": False, "call": None}
        if background_check:
            original_provider = agent.request_model

            async def background_provider(request):
                response = await original_provider(request)
                found = next(
                    (
                        item.get("name")
                        for item in response.get("output", [])
                        if item.get("type") == "function_call"
                        and item.get("name") in ("invoke_creation", "preview_creation")
                    ),
                    None,
                )
                if found and not background["changed"]:
                    # Change the project only after the model has already decided to
                    # submit, reproducing a user edit that arrives in flight.
                    background["changed"] = True
                    background["call"] = found
                    async with db.transaction() as session:
                        current = await session.get(Project, project.id)
                        current.goal = "背景变化后的新目标：改为红色巨轮"
                        current.revision += 1
                return response

            agent.provider = background_provider
        if generate:
            worker_job = asyncio.create_task(worker.run(recover=not bool(resume)))
        agent_job = asyncio.create_task(agent.serve())
        previous = None
        observation_seconds = (
            max(600, settings.image_timeout_seconds + settings.agent_active_seconds + 60)
            if generate
            else 180
            if background_check
            else 600
        )
        for _ in range(observation_seconds):
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
                    "scope": run.scope,
                    "production_operations": run.work_plan.get("production_operations", []),
                    "model_requests": run.model_count,
                    "resumed": bool(resume),
                    "tools": [
                        {
                            "name": c.name,
                            "state": c.state,
                            "error": (c.result or {}).get("error"),
                        }
                        for c in calls
                    ],
                }
                if scope_check:
                    async with db.session() as session:
                        resources = await agent.tools.resources(session, run)
                    report["scope_check_passed"] = (
                        scope_requests_safe
                        and {item["asset_id"] for item in resources["items"]} == {selected[0]}
                        and run.execution_count == 0
                    )
                if background_check:
                    async with db.session() as session:
                        run_tasks = (await session.scalars(select(Task).where(Task.agent_run_id == run.id))).all()
                        executions = (
                            await session.scalars(
                                select(Execution).where(Execution.task_id.in_([task.id for task in run_tasks]))
                            )
                        ).all() if run_tasks else []
                        steps = (
                            await session.scalars(
                                select(CreativeAgentStep).where(CreativeAgentStep.run_id == run.id)
                            )
                        ).all()
                    status_seen = False
                    for step in steps:
                        initial = (step.request_data or {}).get("input") or []
                        try:
                            data = json.loads(initial[0]["content"]) if initial else {}
                        except (KeyError, TypeError, ValueError):
                            continue
                        status = (data.get("current") or {}).get("project_background") or {}
                        if status.get("goal_or_style_changed") is True:
                            status_seen = True
                            break
                    blocked = [
                        call
                        for call in calls
                        if ((call.result or {}).get("error") or {}).get("code") == "project_background_changed"
                    ]
                    report["background"] = {
                        "changed_before_call": background["call"],
                        "changed": background["changed"],
                        "status_seen_by_model": status_seen,
                        "blocked_calls": [{"name": call.name, "state": call.state} for call in blocked],
                        "tasks": len(run_tasks),
                        "executions": len(executions),
                    }
                    report["background_check_passed"] = bool(
                        background["changed"]
                        and blocked
                        and not run_tasks
                        and not executions
                        and run.execution_count == 0
                    )
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
                if background_check:
                    return 0 if report.get("background_check_passed") else 1
                return (
                    0
                    if run.state == "completed" and (
                        report["kind"] == "reply" and report.get("scope_check_passed")
                        if scope_check else report["kind"] == "delivered"
                    )
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true")
    mode.add_argument("--scope", action="store_true")
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--resume", type=Path)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.generate, args.scope, args.resume, args.background)))
