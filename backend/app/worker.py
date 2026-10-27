import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, func, select

from .comfy import file_references
from .conversations import touch_conversation
from .errors import AppError
from .goal_tracking import finish_video_step
from .models import (
    AgentTurn,
    Artifact,
    Asset,
    Execution,
    ImportBatch,
    Message,
    ProjectAsset,
    Task,
    now,
    record_event,
    row_dict,
)
from .schemas import PARAMETER_MODELS
from .storage import atomic_json, file_hash

log = logging.getLogger("aiv.worker")
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "unknown"}
SUBMITTED = {"submitting", "submission_unknown", "queued", "running", "cancel_requested"}


class Worker:
    def __init__(self, db, comfy, registry, storage, settings):
        self.db, self.comfy, self.registry, self.storage, self.settings = db, comfy, registry, storage, settings
        self.wake = asyncio.Event()
        self.stopping = False
        self.collect_locks = {}
        self.background = set()
        self.active_id = None
        self.status = {"connected": False, "error": "正在连接ComfyUI"}
        self.stats = None
        self.checked = 0.0
        self.last_own_finish = None
        self.freed = False

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def health(self, force=False):
        if not force and time.monotonic() - self.checked < 30:
            return self.status
        self.checked = time.monotonic()
        from .models import Event

        async with self.db.transaction() as session:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            maximum = await session.scalar(select(func.max(Event.id))) or 0
            await session.execute(delete(Event).where((Event.created_at < cutoff) | (Event.id <= maximum - 100000)))
        try:
            stats, info = await asyncio.gather(self.comfy.get("/system_stats"), self.comfy.get("/object_info"))
            self.registry.inspect(info)
            version = stats.get("system", {}).get("comfyui_version", "")
            if tuple(int(x) for x in version.split(".")[:3]) < (0, 31, 0):
                self.registry.offline("需要ComfyUI 0.31.0及其定向取消接口。")
                self.status = {
                    "connected": True,
                    "compatible": False,
                    "version": version,
                    "error": "ComfyUI版本低于本产品运行基线",
                }
            else:
                self.status = {"connected": True, "compatible": True, "version": version, "error": None}
            self.stats = stats
        except (httpx.HTTPError, ValueError, OSError) as exc:
            self.registry.offline("ComfyUI未连接，请启动本机服务。")
            self.status = {"connected": False, "error": "无法连接本机ComfyUI", "error_type": type(exc).__name__}
        return self.status

    async def recover(self):
        async with self.db.transaction() as session:
            turns = (await session.scalars(select(AgentTurn).where(AgentTurn.state == "planning"))).all()
            for turn in turns:
                turn.state = "failed"
                turn.error = {"code": "app_interrupted", "message": "应用关闭时方案准备未完成；请重新提交。"}
            from .models import LLMCall

            for call in (await session.scalars(select(LLMCall).where(LLMCall.state == "reserved"))).all():
                call.state = "unknown"
            executions = (
                await session.scalars(
                    select(Execution).where(Execution.execution_state.in_(("preparing", "waiting_external")))
                )
            ).all()
            for execution in executions:
                # preparing is persisted before POST; submitting precedes network send.
                execution.execution_state = "pending"
        await self.storage.recover_receipts()
        await self.storage.reconcile()
        async with self.db.session() as session:
            unfinished = (
                await session.scalars(
                    select(Execution).where(
                        Execution.execution_state.in_(TERMINAL), Execution.storage_state.in_(("pending", "partial"))
                    )
                )
            ).all()
            import_batches = (await session.scalars(select(ImportBatch).where(ImportBatch.state == "running"))).all()
        if import_batches:
            async with self.db.transaction() as session:
                for batch in import_batches:
                    current = await session.get(ImportBatch, batch.id)
                    current.state = "interrupted"
        for execution in unfinished:
            self.spawn(self.collect(execution.id))
        async with self.db.session() as session:
            active = (await session.scalars(select(Task).where(Task.state.in_(("queued", "running", "saving"))))).all()
        for task in active:
            await self.aggregate(task.id)

    async def run(self):
        await self.recover()
        while not self.stopping:
            try:
                await self.health()
                if self.status.get("compatible"):
                    async with self.db.session() as session:
                        execution = await session.scalar(
                            select(Execution)
                            .join(Task)
                            .where(Execution.execution_state.in_(SUBMITTED))
                            .order_by(Task.queue_seq, Execution.candidate_index)
                            .limit(1)
                        )
                        if not execution:
                            execution = await session.scalar(
                                select(Execution)
                                .join(Task)
                                .where(
                                    Execution.execution_state.in_(("pending", "waiting_external")),
                                    Task.cancel_requested.is_(False),
                                    Task.state.in_(("queued", "running")),
                                )
                                .order_by(Task.queue_seq, Execution.candidate_index)
                                .limit(1)
                            )
                    if execution:
                        queue = await self.comfy.queue()
                        if execution.execution_state in SUBMITTED:
                            await self.monitor(execution.id, recovery=True)
                        elif queue.get("queue_running") or queue.get("queue_pending"):
                            await self.update(
                                execution.id,
                                execution_state="waiting_external",
                                progress={"stage": "等待ComfyUI中的其他任务"},
                            )
                            self.wake.clear()
                            with contextlib.suppress(asyncio.TimeoutError):
                                await asyncio.wait_for(self.wake.wait(), timeout=3)
                        else:
                            await self.start(execution.id)
                        continue
                    await self.idle_free()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker iteration failed")
            self.wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=3)

    async def update(self, execution_id, **values):
        async with self.db.transaction() as session:
            execution = await session.get(Execution, execution_id)
            if not execution:
                return
            changed = any(getattr(execution, k) != v for k, v in values.items())
            for key, value in values.items():
                setattr(execution, key, value)
            task = await session.get(Task, execution.task_id)
            if execution.execution_state in (
                "preparing",
                "submitting",
                "queued",
                "running",
                "cancel_requested",
                "submission_unknown",
            ):
                task.state = "running"
            task.updated_at = now()
            if changed:
                record_event(session, "task.updated", task.project_id, task.id, execution_id=execution_id, **values)

    async def start(self, execution_id):
        async with self.db.session() as session:
            execution = await session.get(Execution, execution_id)
            task = await session.get(Task, execution.task_id)
            if task.cancel_requested:
                return
        self.active_id = execution_id
        socket = None
        try:
            self.storage.check_space()
            tool = self.registry.get(execution.tool_id, execution.tool_version)
            if tool["availability"] != "available" or tool["template_hash"] != execution.configuration["template_hash"]:
                raise AppError("tool_changed", "确认后的工具版本、节点或模型已不可用，请查看诊断信息。")
            await self.update(execution_id, execution_state="preparing", progress={"stage": "准备输入素材"})
            uploads = {}
            for item in execution.inputs:
                asset, blob, path = await self.storage.blob_path(item["asset_id"])
                if blob.sha256 != item["blob_id"] or await asyncio.to_thread(file_hash, path) != blob.sha256:
                    raise AppError("input_integrity", "输入素材内容已改变，已停止提交。")
                uploads[item["slot"]] = await self.comfy.upload(path, blob.sha256, blob.extension, "AIVAgent/inputs")
            api = self.registry.render(
                execution.tool_id, execution.tool_version, execution.parameters, uploads, execution.id
            )
            payload = dict(
                prompt=api,
                prompt_id=execution.prompt_id,
                client_id=execution.client_id,
                extra_data={"aiv_execution_id": execution.id},
            )
            await asyncio.to_thread(
                atomic_json, self.settings.data_dir / "executions" / execution.id / "submitted.json", payload
            )
            socket = await self.comfy.connect(execution.client_id)
            async with self.db.transaction() as session:
                current = await session.get(Execution, execution_id)
                parent = await session.get(Task, current.task_id)
                if parent.cancel_requested:
                    current.execution_state = "cancelled"
                    await socket.close()
                    await session.flush()
                    return
                current.submitted_payload = payload
                current.execution_state = "submitting"
            try:
                await self.comfy.submit(payload)
                await self.update(execution_id, execution_state="queued", progress={"stage": "已提交ComfyUI"})
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    try:
                        details = exc.response.json()
                    except ValueError:
                        details = {"message": "提交被服务拒绝"}
                    await self.update(
                        execution_id,
                        execution_state="failed",
                        error={"code": "submission_rejected", "message": "ComfyUI拒绝了执行参数", "details": details},
                        finished_at=now(),
                    )
                    return
                await self.update(
                    execution_id,
                    execution_state="submission_unknown",
                    error={"code": "submission_unknown", "message": "提交响应异常，正在核实是否已受理。"},
                )
            except (httpx.HTTPError, OSError):
                await self.update(
                    execution_id,
                    execution_state="submission_unknown",
                    error={"code": "submission_unknown", "message": "提交响应丢失，正在核实；不会重复提交。"},
                )
            await self.monitor(execution_id, socket=socket)
            socket = None
        except Exception as exc:
            async with self.db.session() as session:
                current = await session.get(Execution, execution_id)
            if current.execution_state in SUBMITTED:
                await self.update(
                    execution_id,
                    execution_state="submission_unknown",
                    error={
                        "code": "monitor_interrupted",
                        "message": "执行监测异常，将继续核实服务器记录；不会重新提交。",
                    },
                )
                log.exception("execution monitoring failed")
                return
            await self.update(
                execution_id,
                execution_state="failed",
                error={
                    "code": getattr(exc, "code", "preparation_failed"),
                    "message": getattr(exc, "message", "输入准备或连接失败"),
                },
                finished_at=now(),
            )
        finally:
            if socket:
                await socket.close()
            self.active_id = None
            await self.aggregate(execution.task_id)

    async def monitor(self, execution_id, socket=None, recovery=False):
        async with self.db.session() as session:
            execution = await session.get(Execution, execution_id)
        self.active_id = execution_id
        absent = 0
        absent_since = None
        delay = 1
        last_progress = 0.0
        last_cancel = 0.0
        try:
            while not self.stopping:
                try:
                    history = await self.comfy.history(execution.prompt_id)
                    if history:
                        await self.complete_from_history(execution_id, history)
                        return
                    queue = await self.comfy.queue()
                    running = any(row[1] == execution.prompt_id for row in queue.get("queue_running", []))
                    queued = any(row[1] == execution.prompt_id for row in queue.get("queue_pending", []))
                    if running or queued:
                        absent = 0
                        absent_since = None
                        async with self.db.session() as session:
                            current = await session.get(Execution, execution_id)
                            parent = await session.get(Task, current.task_id)
                        start = current.started_at or (now() if running else None)
                        timed_out = start and (
                            datetime.fromisoformat(now()) - datetime.fromisoformat(start)
                        ).total_seconds() > current.configuration.get("timeout_seconds", 1800)
                        if parent.cancel_requested or timed_out:
                            await self.update(
                                execution_id,
                                execution_state="cancel_requested",
                                started_at=start,
                                progress={"stage": "正在取消"},
                                error={"code": "timeout", "message": "执行超过时间限制，已请求取消。"}
                                if timed_out
                                else current.error,
                            )
                            if time.monotonic() - last_cancel > 5:
                                await self.comfy.cancel(execution.prompt_id)
                                last_cancel = time.monotonic()
                            if queued:
                                new_queue = await self.comfy.queue()
                                if not any(
                                    row[1] == execution.prompt_id
                                    for row in new_queue.get("queue_pending", []) + new_queue.get("queue_running", [])
                                ):
                                    await self.update(execution_id, execution_state="cancelled", finished_at=now())
                                    await self.collect(execution_id)
                                    return
                        else:
                            await self.update(
                                execution_id,
                                execution_state="running" if running else "queued",
                                started_at=start,
                                error=None,
                            )
                    else:
                        absent += 1
                        absent_since = absent_since or time.monotonic()
                        if absent >= 5 and time.monotonic() - absent_since >= 15:
                            await self.update(
                                execution_id,
                                execution_state="unknown",
                                finished_at=now(),
                                error={
                                    "code": "lost_evidence",
                                    "message": "服务器队列和历史均无此执行记录，无法确认结果；不会自动重做。",
                                },
                            )
                            await self.collect(execution_id)
                            return
                    if socket is None:
                        socket = await self.comfy.connect(execution.client_id)
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=3)
                        if not isinstance(raw, str):
                            continue
                        event = json.loads(raw)
                        data = event.get("data", {})
                        if data.get("prompt_id") not in (None, execution.prompt_id):
                            continue
                        kind = event.get("type")
                        if kind == "executed":
                            await self.register_outputs(execution_id, {str(data["node"]): data.get("output", {})})
                            self.spawn(self.collect(execution_id))
                        elif kind == "progress" and time.monotonic() - last_progress >= 1:
                            await self.update(
                                execution_id,
                                progress={
                                    "stage": "采样中",
                                    "node": data.get("node"),
                                    "value": data.get("value"),
                                    "max": data.get("max"),
                                },
                            )
                            last_progress = time.monotonic()
                        elif kind == "executing" and data.get("node"):
                            await self.update(execution_id, progress={"stage": "执行工作流", "node": data["node"]})
                        elif kind in ("execution_success", "execution_error", "execution_interrupted"):
                            # History is committed after terminal WS events.
                            for wait in (0.5, 1, 2, 4, 8):
                                await asyncio.sleep(wait)
                                history = await self.comfy.history(execution.prompt_id)
                                if history:
                                    await self.complete_from_history(execution_id, history)
                                    return
                    except asyncio.TimeoutError:
                        pass
                    delay = 1
                except (httpx.HTTPError, OSError, websockets_error()) as exc:
                    if socket:
                        with contextlib.suppress(Exception):
                            await socket.close()
                    socket = None
                    await self.update(
                        execution_id, progress={"stage": "连接中断，正在核实和重连", "error_type": type(exc).__name__}
                    )
                    await asyncio.sleep(delay)
                    delay = min(15, delay * 2)
        finally:
            if socket:
                await socket.close()
            self.active_id = None
            await self.aggregate(execution.task_id)

    async def complete_from_history(self, execution_id, history):
        messages = history.get("status", {}).get("messages", [])
        kinds = [m[0] for m in messages]
        error = None
        if "execution_interrupted" in kinds:
            state = "cancelled"
        elif history.get("status", {}).get("status_str") == "success":
            state = "succeeded"
        else:
            state = "failed"
            data = next((m[1] for m in messages if m[0] == "execution_error"), {})
            error = {
                "code": "execution_error",
                "message": data.get("exception_message", "工作流执行失败"),
                "node_id": data.get("node_id"),
                "node_type": data.get("node_type"),
            }
        actual = history.get("prompt", [None, None, None])[2]
        await self.update(
            execution_id,
            execution_state=state,
            history=history,
            executed_payload=actual,
            error=error,
            finished_at=now(),
            progress={"stage": "正在保存结果"},
        )
        await asyncio.to_thread(
            atomic_json, self.settings.data_dir / "executions" / execution_id / "history.json", history
        )
        await self.register_outputs(execution_id, history.get("outputs", {}))
        await self.collect(execution_id)
        self.last_own_finish = time.monotonic()
        self.freed = False

    async def register_outputs(self, execution_id, outputs):
        async with self.db.transaction() as session:
            execution = await session.get(Execution, execution_id)
            task = await session.get(Task, execution.task_id)
            roles = self.registry.get(execution.tool_id, execution.tool_version)["roles"]
            for node, field, index, ref in file_references(outputs):
                artifact = await session.scalar(
                    select(Artifact).where(
                        Artifact.execution_id == execution_id,
                        Artifact.node_id == node,
                        Artifact.field == field,
                        Artifact.item_index == index,
                    )
                )
                if not artifact:
                    artifact = Artifact(
                        execution_id=execution_id,
                        node_id=node,
                        field=field,
                        item_index=index,
                        role=roles.get(node, "intermediate"),
                        reference=ref,
                    )
                    session.add(artifact)
                    record_event(session, "artifact.pending", task.project_id, execution_id, reference=ref)

    async def collect(self, execution_id):
        lock = self.collect_locks.setdefault(execution_id, asyncio.Lock())
        async with lock:
            async with self.db.session() as session:
                execution = await session.get(Execution, execution_id)
                task = await session.get(Task, execution.task_id)
                artifacts = (
                    await session.scalars(
                        select(Artifact).where(Artifact.execution_id == execution_id).order_by(Artifact.created_at)
                    )
                ).all()
            # Temporary outputs are fetched eagerly; final reconciliation can relate duplicates.
            artifacts.sort(key=lambda a: a.reference.get("type") != "temp")
            for artifact in artifacts:
                if artifact.state == "saved":
                    try:
                        await self.storage.blob_path(artifact.asset_id)
                        continue
                    except AppError:
                        pass
                staging = self.settings.data_dir / "staging" / (artifact.id + ".artifact")
                try:
                    self.storage.check_space()
                    async with self.db.transaction() as session:
                        current = await session.get(Artifact, artifact.id)
                        current.staging_path = staging.name
                        parent = await session.get(Task, execution.task_id)
                        if execution.execution_state in TERMINAL and parent.state != "saving":
                            parent.state = "saving"
                            record_event(session, "task.updated", parent.project_id, parent.id, state="saving")
                    await self.comfy.download(
                        artifact.reference, staging, self.settings.max_import_file_bytes, self.storage.check_space
                    )
                    source = dict(
                        tool_id=execution.tool_id,
                        tool_name=self.registry.get(execution.tool_id)["name"],
                        parameters=execution.parameters,
                        reference=artifact.reference,
                        task_step=execution.configuration.get("step_label"),
                        internal_preparation=execution.configuration.get("step_index") == 0,
                    )
                    description = (
                        execution.parameters.get("prompt") or execution.parameters.get("instruction") or "高清放大"
                    )
                    await self.storage.ingest(
                        staging,
                        artifact.reference["filename"],
                        task.project_id,
                        origin="generated",
                        role=artifact.role,
                        description=description,
                        source_info=source,
                        execution_id=execution_id,
                        candidate_index=0 if execution.configuration.get("step_count") else execution.candidate_index,
                        inputs=execution.inputs,
                        artifact_id=artifact.id,
                    )
                except Exception as exc:
                    async with self.db.transaction() as session:
                        current = await session.get(Artifact, artifact.id)
                        current.state = (
                            "pending"
                            if isinstance(exc, AppError) and exc.code == "disk_space"
                            else "missing"
                            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404
                            else "failed"
                        )
                        current.error = getattr(exc, "message", str(exc))[:2000]
            await self.relate_previews(execution_id, task.project_id)
            async with self.db.transaction() as session:
                current = await session.get(Execution, execution_id)
                records = (await session.scalars(select(Artifact).where(Artifact.execution_id == execution_id))).all()
                saved = sum(a.state == "saved" for a in records)
                missing = any(a.state == "missing" for a in records)
                final = any(a.role == "final" and a.state == "saved" for a in records)
                current.storage_state = (
                    "complete"
                    if records and saved == len(records) and (final or current.execution_state != "succeeded")
                    else "missing"
                    if missing
                    else "partial"
                    if saved
                    else "pending"
                    if records or current.execution_state == "succeeded"
                    else "none"
                )
                current.configuration = {**current.configuration, "collection_attempted": True}
                manifest = dict(
                    execution_id=execution_id,
                    storage_state=current.storage_state,
                    artifacts=[row_dict(a) for a in records],
                )
            await asyncio.to_thread(
                atomic_json, self.settings.data_dir / "executions" / execution_id / "artifacts.json", manifest
            )
        await self.aggregate(execution.task_id)

    async def relate_previews(self, execution_id, project_id):
        async with self.db.transaction() as session:
            assets = (await session.scalars(select(Asset).where(Asset.execution_id == execution_id))).all()
            for preview in (a for a in assets if a.role == "preview" and a.pixel_hash):
                final = next((a for a in assets if a.role == "final" and a.pixel_hash == preview.pixel_hash), None)
                if final:
                    membership = await session.get(ProjectAsset, (project_id, preview.id))
                    membership.removed_at = now()
                    artifact = await session.scalar(select(Artifact).where(Artifact.asset_id == preview.id))
                    artifact.representation_of = final.id

    async def advance_steps(self, session, task, executions):
        by_id = {item.id: item for item in executions}
        waiting_storage = False
        for execution in executions:
            if execution.execution_state != "blocked":
                continue
            parent = by_id.get(execution.configuration.get("depends_on_execution_id"))
            if not parent:
                execution.execution_state = "failed"
                execution.error = {"code": "dependency_missing", "message": "缺少起始画面的执行记录。"}
                continue
            if task.cancel_requested or parent.execution_state in TERMINAL - {"succeeded"}:
                execution.execution_state, execution.finished_at = "cancelled", now()
                continue
            if parent.execution_state != "succeeded":
                continue
            if parent.storage_state != "complete":
                waiting_storage |= parent.storage_state in ("partial", "missing") or (
                    parent.storage_state == "pending" and parent.configuration.get("collection_attempted", False)
                )
                continue
            assets = list(
                await session.scalars(
                    select(Asset)
                    .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                    .where(
                        Asset.execution_id == parent.id,
                        Asset.role == "final",
                        Asset.media_type == "image",
                        Asset.presence == "available",
                        ProjectAsset.project_id == task.project_id,
                        ProjectAsset.removed_at.is_(None),
                    )
                )
            )
            try:
                if len(assets) != 1:
                    raise AppError("dependency_output", "起始画面没有唯一可用的图片，已停止后续生成。")
                asset = assets[0]
                slot = execution.configuration["input_slot"]
                values = {
                    k: v
                    for k, v in execution.parameters.items()
                    if k in PARAMETER_MODELS[execution.tool_id].model_fields
                }
                params, _, _ = self.registry.normalize(
                    execution.tool_id,
                    values,
                    {slot: row_dict(asset)},
                    self.settings,
                )
                execution.parameters = params
                execution.inputs = [dict(slot=slot, asset_id=asset.id, blob_id=asset.blob_id)]
                execution.execution_state = "pending"
                execution.configuration = {**execution.configuration, "resolved_dependency_asset_id": asset.id}
                execution.progress = {"stage": "起始画面已保存，自动继续生成视频"}
                record_event(
                    session,
                    "task.updated",
                    task.project_id,
                    task.id,
                    execution_id=execution.id,
                    state="queued",
                    step_index=1,
                )
                self.wake.set()
            except AppError as exc:
                execution.execution_state, execution.finished_at = "failed", now()
                execution.error = {"code": exc.code, "message": exc.message}
        return waiting_storage

    async def aggregate(self, task_id):
        async with self.db.transaction() as session:
            task = await session.get(Task, task_id)
            if not task:
                return
            executions = (await session.scalars(select(Execution).where(Execution.task_id == task_id))).all()
            waiting_storage = await self.advance_steps(session, task, executions)
            if (
                any(e.execution_state in ("failed", "unknown", "interrupted") for e in executions)
                or task.cancel_requested
            ):
                for execution in executions:
                    if execution.execution_state in ("pending", "waiting_external", "blocked"):
                        execution.execution_state = "cancelled"
                        execution.finished_at = now()
            all_terminal = all(e.execution_state in TERMINAL for e in executions)
            if all_terminal:
                if any(
                    e.execution_state in ("unknown", "interrupted")
                    or e.storage_state in ("pending", "partial", "missing")
                    for e in executions
                ):
                    state = "needs_attention"
                elif any(e.execution_state == "failed" for e in executions):
                    state = "failed"
                elif any(e.execution_state == "cancelled" for e in executions):
                    state = "cancelled"
                else:
                    state = "completed"
            elif waiting_storage:
                state = "needs_attention"
            elif any(e.execution_state in SUBMITTED or e.execution_state == "preparing" for e in executions):
                state = "running"
            elif any(e.execution_state == "succeeded" and e.storage_state == "pending" for e in executions):
                state = "saving"
            else:
                state = "queued"
            previous = task.state
            task.state = state
            task.updated_at = now()
            if all_terminal:
                task.finished_at = task.finished_at or now()
                task.error = next((e.error for e in executions if e.error), None)
            if previous != state:
                record_event(session, "task.updated", task.project_id, task_id, state=state)
                if all_terminal or waiting_storage:
                    if task.turn_id:
                        turn = await session.get(AgentTurn, task.turn_id)
                        turn.state = "completed" if state == "completed" else "failed"
                    saved = await session.scalar(
                        select(func.count())
                        .select_from(Asset)
                        .where(Asset.execution_id.in_([e.id for e in executions]), Asset.role != "preview")
                    )
                    content = f"本次操作{ {'completed': '完成', 'failed': '失败', 'cancelled': '已取消', 'needs_attention': '需要处理'}[state] }，已保留{saved}份素材。"
                    if state == "needs_attention":
                        content += "请查看任务详情；可以补存已有结果，补存不会重新生成。"
                    else:
                        content += "请查看结果，再告诉我下一步想怎么调整。"
                    details = {}
                    video_step = await finish_video_step(session, task, executions, state)
                    if video_step:
                        content, details = video_step
                    session.add(
                        Message(
                            project_id=task.project_id,
                            conversation_id=task.conversation_id,
                            turn_id=task.turn_id,
                            role="assistant",
                            content=content,
                            task_id=task_id,
                            details=details,
                        )
                    )
                    record_event(session, "message.created", task.project_id, task_id)
                    await touch_conversation(session, task.conversation_id)

    async def idle_free(self):
        if self.freed or not self.last_own_finish or self.settings.auto_free_after_idle_seconds <= 0:
            return
        if time.monotonic() - self.last_own_finish >= self.settings.auto_free_after_idle_seconds:
            queue = await self.comfy.queue()
            if not queue.get("queue_running") and not queue.get("queue_pending"):
                await self.comfy.free()
                self.freed = True

    async def stop(self):
        self.stopping = True
        self.wake.set()
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)


def websockets_error():
    from websockets.exceptions import WebSocketException

    return WebSocketException
