"""Durable, native Responses tool loop, independent of the guided assistant."""

import asyncio
import json
import logging
import time

from openai import APIConnectionError, APIStatusError
from pydantic import ValidationError
from sqlalchemy import select

from .conversations import resolve_conversation, touch_conversation
from .creative_agent_history import capture_history, history_page, request_resource_ids, resource_ids
from .creative_agent_media import image_part
from .creative_agent_schema import CONTROL_TOOLS, Answer, Finish, StartRun, Tasks, tool_definitions
from .creative_agent_scope import TERMINAL_RUNS, require_running, scoped_asset
from .creative_agent_tools import AgentTools, failure, success
from .errors import AppError
from .models import (
    CreativeAgentCall,
    CreativeAgentRun,
    CreativeAgentStep,
    Execution,
    Idempotency,
    Message,
    Task,
    now,
    record_event,
    row_dict,
)
from .plans import ACTIVE_TASKS, digest, require_project
from .vision import linked_asset_ids

log = logging.getLogger("aiv.agent")


class CreativeAgent:
    def __init__(self, state):
        self.state = state
        self.db, self.settings = state.db, state.settings
        self.tools = AgentTools(state)
        self.jobs = {}
        self.wake = asyncio.Event()
        self.stopping = False
        self.active_ticks = {}
        # Injectable provider for deterministic protocol/recovery tests.
        self.provider = self.request_model

    async def start_run(self, project_id, request: StartRun, *, public_access=False):
        payload = digest({"project_id": project_id, **request.model_dump(), "public_access": public_access})
        async with self.db.transaction() as session:
            old = await session.get(Idempotency, request.idempotency_key)
            if old:
                if old.scope != "agent_start" or old.payload_hash != payload:
                    raise AppError("idempotency_conflict", "提交标识已用于不同请求。", 409)
                return old.result
            project = await require_project(session, project_id, writable=True)
            conversation = await resolve_conversation(session, project_id, request.conversation_id, writable=True)
            if conversation.mode != "autonomous":
                raise AppError("conversation_mode", "请在自主创作对话中发起 Agent。", 409)
            active = await session.scalar(
                select(CreativeAgentRun.id).where(
                    CreativeAgentRun.conversation_id == conversation.id, CreativeAgentRun.state.not_in(TERMINAL_RUNS)
                )
            )
            if active:
                raise AppError(
                    "agent_run_active", "当前创作尚未结束。首版请在完成或取消后发起修改。", 409, {"run_id": active}
                )
            if request.parent_run_id:
                parent = await session.get(CreativeAgentRun, request.parent_run_id)
                if not parent or parent.conversation_id != conversation.id or parent.state not in TERMINAL_RUNS:
                    raise AppError("parent_run", "关联创作必须是本对话已结束的 Run。", 409)
            selected = list(
                dict.fromkeys(
                    [
                        *request.selected_asset_ids,
                        *linked_asset_ids(request.content, self.settings.port, self.settings.public_origin),
                    ]
                )
            )
            from .models import ProjectAsset

            for aid in selected:
                membership = await session.get(ProjectAsset, (project_id, aid))
                if not membership or membership.removed_at:
                    raise AppError("input_reference", "指定素材不在当前项目。", 409)
                await self.state.storage.blob_path(aid, session)
            run = CreativeAgentRun(
                project_id=project_id,
                conversation_id=conversation.id,
                goal=request.content.strip(),
                parent_run_id=request.parent_run_id,
                goal_snapshot={
                    "original": request.content,
                    "project": {
                        "name": project.name,
                        "goal": project.goal,
                        "style": project.style,
                        "revision": project.revision,
                    },
                },
                scope={
                    "selected": selected,
                    "use_key_assets": request.use_key_assets,
                    "image_roles": {k: v for k, v in request.image_roles.items() if k in selected},
                    "reference_policy": {"state": "pending"},
                },
                access={"public_access": public_access, "can_manage": not public_access},
                limits={
                    "executions": self.settings.agent_max_executions,
                    "issue_reworks": self.settings.agent_max_issue_reworks,
                    "model_requests": self.settings.agent_max_model_requests,
                    "tool_calls": self.settings.agent_max_tool_calls,
                    "no_progress": self.settings.agent_max_no_progress,
                    "active_ms": self.settings.agent_active_seconds * 1000,
                },
            )
            session.add(run)
            await session.flush()
            run.goal_snapshot = {**run.goal_snapshot, "conversation_history": await capture_history(session, run)}
            session.add(
                Message(
                    project_id=project_id,
                    conversation_id=conversation.id,
                    role="user",
                    content=request.content,
                    details={
                        "agent_run_id": run.id,
                        "selected_asset_ids": selected,
                        "image_roles": run.scope["image_roles"],
                    },
                )
            )
            if conversation.auto_title:
                conversation.title = " ".join(request.content.split())[:36]
                conversation.auto_title = False
            result = {"run_id": run.id, "state": run.state, "conversation_id": conversation.id}
            session.add(
                Idempotency(key=request.idempotency_key, scope="agent_start", payload_hash=payload, result=result)
            )
            record_event(session, "agent.updated", project_id, run.id)
            await touch_conversation(session, conversation.id)
        self.wake.set()
        return result

    async def request_model(self, request):
        planner = self.state.planner
        key = await planner.key()
        if not key:
            raise AppError("llm_key_missing", "请先在设置中配置模型 API Key。", 409)
        session_id = request["_conversation_id"]
        payload = {field: value for field, value in request.items() if field != "_conversation_id"}
        response = await planner.request_responses(key, payload, session_id=session_id)
        result = response.model_dump(mode="json")
        # Native protocol is persisted; consumption and fees are deliberately not recorded.
        result.pop("usage", None)
        return result

    async def recover(self):
        async with self.db.transaction() as session:
            for step in (
                await session.scalars(select(CreativeAgentStep).where(CreativeAgentStep.state == "requesting"))
            ).all():
                # No complete saved response => no tool effects from that request.
                step.error = {
                    **(step.error or {}),
                    "code": "model_response_unknown",
                    "message": "服务重启前请求未保存完整响应。",
                }
            # Reconcile effects committed before the native tool return was
            # persisted, including controls which deliberately stop scheduling.
            for call in (
                await session.scalars(select(CreativeAgentCall).where(CreativeAgentCall.state == "executing"))
            ).all():
                run = await session.get(CreativeAgentRun, call.run_id)
                step = await session.get(CreativeAgentStep, call.step_id)
                finish_payload = None
                if call.name == "finish_run":
                    try:
                        finish_payload = Finish.model_validate_json(call.arguments).model_dump()
                    except ValidationError:
                        pass  # Invalid unexecuted arguments are handled by normal dispatch.
                if call.name == "finish_run" and run.completion is not None and run.completion == finish_payload:
                    call.result = success(
                        {
                            "state": run.state,
                            "kind": run.completion["kind"],
                            "asset_ids": run.completion.get("asset_ids", []),
                        }
                    )
                    call.state, step.state = "completed", "completed"
                elif call.name == "ask_user":
                    messages = (
                        await session.scalars(
                            select(Message).where(
                                Message.conversation_id == run.conversation_id, Message.role == "assistant"
                            )
                        )
                    ).all()
                    if any(
                        m.details.get("agent_run_id") == run.id and m.details.get("question_call_id") == call.call_id
                        for m in messages
                    ):
                        call.state = "waiting"
                elif call.name == "wait_for_tasks" and run.state == "waiting_task":
                    call.state = "waiting"
                elif call.name == "invoke_creation":
                    task = await session.scalar(select(Task).where(Task.agent_call_id == call.id))
                    if task:
                        call.result, call.state = await self.tools.creation_result(session, task), "completed"
                elif call.name == "write_document":
                    from uuid import NAMESPACE_URL, uuid5

                    from .models import Asset

                    asset = await session.get(Asset, str(uuid5(NAMESPACE_URL, "aiv-agent-document:" + call.id)))
                    if asset and asset.source_info.get("agent_call_id") == call.id:
                        call.asset_id = asset.id
                        call.result = success(
                            {
                                "asset_id": asset.id,
                                "blob_id": asset.blob_id,
                                "parent_asset_id": asset.source_info.get("parent_asset_id"),
                            },
                            effects=[{"type": "document", "id": asset.id}],
                        )
                        call.state, run.intent = "completed", "creation"
            for run in (
                await session.scalars(select(CreativeAgentRun).where(CreativeAgentRun.state == "running"))
            ).all():
                run.state = "queued"

    async def serve(self):
        await self.recover()
        while not self.stopping:
            self.wake.clear()
            await self.charge_active()
            async with self.db.session() as session:
                rows = (
                    await session.scalars(
                        select(CreativeAgentRun).where(
                            CreativeAgentRun.state.in_(("queued", "running", "waiting_task", "cancelling"))
                        )
                    )
                ).all()
            for run in rows:
                if run.id not in self.jobs:
                    task = asyncio.create_task(self.drive(run.id))
                    self.jobs[run.id] = task
                    task.add_done_callback(lambda _, rid=run.id: self.jobs.pop(rid, None))
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def stop(self):
        self.stopping = True
        self.wake.set()
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def charge_active(self, run_id=None, *, finish=False):
        ids = [run_id] if run_id else list(self.active_ticks)
        async with self.db.transaction() as session:
            for rid in ids:
                tick = self.active_ticks.get(rid)
                if tick is None:
                    continue
                end = time.monotonic()
                run = await session.get(CreativeAgentRun, rid)
                if run:
                    run.active_ms += max(0, round((end - tick) * 1000))
                    run.updated_at = now()
                if finish:
                    self.active_ticks.pop(rid, None)
                else:
                    self.active_ticks[rid] = end

    async def attention(self, run_id, code, message):
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, run_id)
            if run and run.state not in (*TERMINAL_RUNS, "paused", "cancelling"):
                run.state = "needs_attention"
                run.stop_reason = {"code": code, "message": message}
                self.tools.message(session, run, message)
                record_event(session, "agent.updated", run.project_id, run.id)

    async def drive(self, run_id):
        try:
            for _ in range(240):
                async with self.db.transaction() as session:
                    run = await session.get(CreativeAgentRun, run_id)
                    if not run or run.state not in ("queued", "running", "waiting_task", "cancelling"):
                        return
                    if run.state == "queued":
                        run.state = "running"
                        record_event(session, "agent.updated", run.project_id, run.id)
                    version = run.control_version
                if run.state == "cancelling":
                    await self.cancel_tasks(run_id)
                    return
                if run.state == "waiting_task":
                    if not await self.wake_wait(run_id):
                        return
                    continue
                if run.active_ms >= run.limits["active_ms"]:
                    await self.attention(run_id, "active_time_limit", "自主推进时长已达上限，已有成果已保留。")
                    return
                if run.no_progress >= run.limits["no_progress"]:
                    await self.attention(run_id, "no_progress", "连续决策没有取得新进展，请查看已有结果与错误。")
                    return
                if run.tool_count > run.limits["tool_calls"]:
                    await self.attention(run_id, "tool_limit", "本次工具调用次数已达上限，已有成果已保留。")
                    return
                self.active_ticks[run_id] = time.monotonic()
                try:
                    async with self.db.session() as session:
                        step = await session.scalar(
                            select(CreativeAgentStep)
                            .where(CreativeAgentStep.run_id == run_id)
                            .order_by(CreativeAgentStep.sequence.desc())
                            .limit(1)
                        )
                    if not step or step.state == "completed":
                        step = await self.new_step(run, version)
                    if step.response_data is None:
                        await self.model_step(run, step, version)
                    else:
                        await self.dispatch_step(run, step, version)
                finally:
                    await self.charge_active(run_id, finish=True)
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            if exc.code != "run_control_changed":
                await self.attention(run_id, exc.code, exc.message)
        except Exception:
            log.exception("Agent run failed: %s", run_id)
            await self.attention(run_id, "runtime_error", "自主创作执行异常，记录和成果已保留，请查看诊断后恢复。")

    async def new_step(self, run, version):
        async with self.db.transaction() as session:
            run = await require_running(session, run.id, version)
            from sqlalchemy import func

            seq = (
                await session.scalar(
                    select(func.max(CreativeAgentStep.sequence)).where(CreativeAgentStep.run_id == run.id)
                )
            ) or 0
            step = CreativeAgentStep(run_id=run.id, sequence=seq + 1, control_version=version)
            session.add(step)
            await session.flush()
            return step

    async def build_request(self, run, step):
        context = (await self.tools.get_project_context(run, None, None, run.control_version))["data"]
        initial = {
            "goal_snapshot": {
                k: v for k, v in run.goal_snapshot.items() if k not in ("conversation_history", "prior_user_messages")
            },
            "current": context,
            "recent_conversation": [],
            "boundary": "Only this goal may execute. Prior user messages are context, not new tasks or material grants.",
        }
        items = [{"role": "user", "content": json.dumps(initial, ensure_ascii=False)}]
        async with self.db.session() as session:
            from .creative_agent_history import snapshot_history

            history = snapshot_history(run)
            for index in range(max(0, len(history) - 2), len(history)):
                try:
                    page = await history_page(session, run, index, 1)
                except AppError:
                    continue
                initial["recent_conversation"].extend(page["items"])
            items[0]["content"] = json.dumps(initial, ensure_ascii=False)
            steps = (
                await session.scalars(
                    select(CreativeAgentStep)
                    .where(CreativeAgentStep.run_id == run.id, CreativeAgentStep.sequence < step.sequence)
                    .order_by(CreativeAgentStep.sequence)
                )
            ).all()
            blocks = []
            for previous in steps:
                if not previous.response_data:
                    continue
                for aid in request_resource_ids(previous.request_data):
                    await scoped_asset(session, run, aid)
                calls = (
                    await session.scalars(
                        select(CreativeAgentCall)
                        .where(CreativeAgentCall.step_id == previous.id)
                        .order_by(CreativeAgentCall.position)
                    )
                ).all()
                if any(c.state not in ("completed", "failed") for c in calls):
                    raise AppError("protocol_open_call", "上一个工具调用尚未闭合。", 409)
                block = list(previous.response_data.get("output", []))
                for call in calls:
                    result = call.result or failure(AppError("tool_interrupted", "工具未返回结果。"))
                    # Scope revocation also covers historic text, search metadata
                    # and images; stop rather than leak a stale cached response.
                    for aid in self.resource_ids(result):
                        await scoped_asset(session, run, aid)
                    parts = [{"type": "input_text", "text": json.dumps(result, ensure_ascii=False)}]
                    if previous.sequence == step.sequence - 1:
                        for resource in result.get("resources", []):
                            if resource.get("kind") == "observation":
                                await scoped_asset(session, run, resource["asset_id"])
                                for frame in resource["frames"]:
                                    path = (
                                        self.settings.data_dir
                                        / "previews"
                                        / "agent"
                                        / resource["call_id"]
                                        / frame["file"]
                                    )
                                    parts.append(await asyncio.to_thread(image_part, path, frame["sha256"]))
                    block.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": parts if len(parts) > 1 else parts[0]["text"],
                        }
                    )
                blocks.append(block)
        # Bounded context without usage accounting. Keep whole native call/result
        # blocks together. Durable facts and references above survive compaction.
        request = {
            "model": self.settings.llm_model,
            "instructions": self.tools.guide_text,
            "tools": tool_definitions(),
            "max_output_tokens": self.settings.max_output_tokens,
            "reasoning": {"effort": self.settings.reasoning_effort},
            "input": items,
        }
        limit = max(16000, self.settings.max_input_tokens - self.settings.max_output_tokens)
        kept = list(blocks)
        while True:
            request["input"] = [*items, *[entry for block in kept for entry in block]]
            if self.request_size(request) <= limit:
                break
            if initial["recent_conversation"]:
                initial["recent_conversation"].pop(0)
                items[0]["content"] = json.dumps(initial, ensure_ascii=False)
                continue
            if len(kept) <= 1:
                raise AppError("context_limit", "目标或当前工具结果超过安全上下文范围，请缩小本次任务。", 409)
            kept.pop(0)
        if len(kept) != len(blocks):
            request["input"].insert(
                1,
                {
                    "role": "user",
                    "content": "较早的已闭合调用已整理。原始目标、当前计划、执行计数与问题仍在 current；文档/素材/任务须按 ID 重新读取。",
                },
            )
        images = sum(
            1
            for item in request["input"]
            for part in (item.get("output", []) if isinstance(item.get("output"), list) else [])
            if part.get("type") == "input_image"
        )
        if images > self.settings.vision_max_images:
            raise AppError("vision_limit", "同一响应的观察图过多，请减少每次采样数量。", 409)
        return {**request, "_conversation_id": run.conversation_id}

    @staticmethod
    def resource_ids(value):
        return resource_ids(value)

    @staticmethod
    def progress_fingerprint(call):
        """Compare new evidence/effects, excluding changing protocol identities.

        A new call ID or a larger request counter is not creative progress.
        """

        def facts(value):
            if isinstance(value, dict):
                return {k: facts(v) for k, v in value.items() if k not in ("call_id", "evidence_call_id")}
            if isinstance(value, list):
                return [facts(v) for v in value]
            return value

        result = facts(call.result or {})
        if call.name == "get_project_context":
            result.get("data", {}).pop("used", None)
            result.get("data", {}).pop("work_plan", None)
        elif call.name == "write_document":
            result = {"blob_id": result.get("data", {}).get("blob_id")}
        elif call.name == "inspect_media":
            result.get("data", {}).pop("purpose", None)
            result.get("data", {}).get("parameters", {}).pop("purpose", None)
        return digest({"name": call.name, "facts": result})

    @staticmethod
    def request_size(value):
        if isinstance(value, dict):
            if value.get("type") == "input_image":
                return 4096  # conservative body/window reserve, never a consumption statistic
            return sum(len(str(k).encode()) + CreativeAgent.request_size(v) for k, v in value.items())
        if isinstance(value, list):
            return sum(CreativeAgent.request_size(v) for v in value)
        return len(str(value).encode("utf-8"))

    async def model_step(self, run, step, version):
        request = await self.build_request(run, step)
        for _ in range(3):
            delay = 0
            async with self.db.transaction() as session:
                current = await require_running(session, run.id, version)
                stored = await session.get(CreativeAgentStep, step.id)
                if stored.attempts >= 3:
                    raise AppError("model_retry_limit", "模型临时请求重试已达上限，请查看网络后恢复或结束。", 409)
                if current.model_count >= current.limits["model_requests"]:
                    raise AppError("model_limit", "本次模型请求次数已达上限，已有成果已保留。", 409)
                attempt = stored.attempts
                if attempt:
                    delay = max((1, 3)[min(attempt - 1, 1)], (stored.error or {}).get("retry_after_seconds", 0))
                if delay * 1000 > current.limits["active_ms"] - current.active_ms or delay > 60:
                    raise AppError("retry_later", "供应商建议等待时间较长，请稍后明确恢复。", 409)
            if delay:
                await asyncio.sleep(delay)
            try:
                # Version is checked again after backoff, before a new request.
                async with self.db.transaction() as session:
                    current = await require_running(session, run.id, version)
                    stored = await session.get(CreativeAgentStep, step.id)
                    current.model_count += 1
                    stored.attempts += 1
                    stored.control_version = version
                    stored.request_data = {
                        **self.audit_request(request),
                        "_manual_retry_groups": stored.request_data.get("_manual_retry_groups", []),
                    }
                response = await self.provider(request)
                response.pop("usage", None)
                await self.save_response(run.id, step.id, response)
                return
            except (APIConnectionError, APIStatusError, TimeoutError) as exc:
                retryable = (
                    not isinstance(exc, APIStatusError) or exc.status_code in (408, 429) or exc.status_code >= 500
                )
                async with self.db.transaction() as session:
                    stored = await session.get(CreativeAgentStep, step.id)
                    stored.error = {"code": "model_request", "type": type(exc).__name__, "retryable": retryable}
                    if isinstance(exc, APIStatusError):
                        try:
                            delay = max(0, float(exc.response.headers.get("retry-after", "0")))
                        except ValueError:
                            from datetime import datetime, timezone
                            from email.utils import parsedate_to_datetime

                            try:
                                delay = max(
                                    0,
                                    (
                                        parsedate_to_datetime(exc.response.headers["retry-after"])
                                        - datetime.now(timezone.utc)
                                    ).total_seconds(),
                                )
                            except (ValueError, KeyError, TypeError):
                                delay = 0
                        stored.error = {**stored.error, "retry_after_seconds": delay}
                if not retryable:
                    raise AppError("llm_request", "模型请求被拒绝，请检查密钥、账户和请求配置。", 502) from exc
        raise AppError("model_retry_limit", "模型请求在有限重试后仍未完成，已有记录和成果已保留。", 502)

    @staticmethod
    def audit_request(request):
        copy = json.loads(json.dumps(request))
        for item in copy.get("input", []):
            for key in ("content", "output"):
                if isinstance(item.get(key), list):
                    for part in item[key]:
                        if part.get("type") == "input_image":
                            part["image_url"] = "[ephemeral evidence bytes omitted]"
        return copy

    async def save_response(self, run_id, step_id, response):
        async with self.db.transaction() as session:
            step = await session.get(CreativeAgentStep, step_id)
            if step.response_data is not None:
                return
            step.response_data = response
            step.state = "ready"
            calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
            if response.get("status") != "completed":
                step.state = "incomplete"
                return
            ids = [c.get("call_id") for c in calls]
            if not all(ids) or len(set(ids)) != len(ids):
                step.state = "invalid_protocol"
                return
            conflict = len(calls) > 1 and any(c.get("name") in CONTROL_TOOLS for c in calls)
            run = await session.get(CreativeAgentRun, run_id)
            run.tool_count += len(calls)
            exceeded = run.tool_count > run.limits["tool_calls"]
            ended = run.state in (*TERMINAL_RUNS, "cancelling")
            for index, item in enumerate(calls):
                error = (
                    AppError("batch_conflict", "控制类工具必须独占响应批次，本批全部未执行。")
                    if conflict
                    else (
                        AppError("tool_limit", "工具调用次数已达上限，本批未执行。")
                        if exceeded
                        else AppError("run_cancelled", "任务已取消，本批未执行。")
                        if ended
                        else None
                    )
                )
                session.add(
                    CreativeAgentCall(
                        run_id=run_id,
                        step_id=step.id,
                        call_id=item["call_id"],
                        position=index,
                        name=item.get("name", ""),
                        arguments=item.get("arguments") or "{}",
                        state="failed" if error else "pending",
                        result=failure(error) if error else None,
                    )
                )

    async def dispatch_step(self, run, step, version):
        if step.state in ("incomplete", "invalid_protocol"):
            raise AppError("model_incomplete", "模型未返回完整合法的工具响应，未执行其中的调用。", 409)
        async with self.db.session() as session:
            calls = (
                await session.scalars(
                    select(CreativeAgentCall)
                    .where(CreativeAgentCall.step_id == step.id)
                    .order_by(CreativeAgentCall.position)
                )
            ).all()
        text = "\n".join(
            part.get("text", "")
            for item in step.response_data.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ).strip()
        if not calls:
            if run.intent == "creation":
                raise AppError("unfinished_creation", "Agent 尚未提交完整交付检查，已保留当前成果与执行记录。", 409)
            if not text:
                raise AppError("empty_response", "模型没有返回内容或工具调用。", 409)
            async with self.db.transaction() as session:
                current = await require_running(session, run.id, version)
                current.state, current.finished_at = "completed", now()
                current.completion = {"kind": "reply", "message": text}
                self.tools.message(session, current, text, {"agent_run_id": run.id, "kind": "reply"})
                stored = await session.get(CreativeAgentStep, step.id)
                stored.state = "completed"
                record_event(session, "agent.updated", current.project_id, run.id)
            return
        for call in calls:
            if call.state in ("completed", "failed"):
                continue
            async with self.db.transaction() as session:
                current = await require_running(session, run.id, version)
                stored = await session.get(CreativeAgentCall, call.id)
                stored.state = "executing"
            try:
                result = await self.tools.dispatch(run.id, call, version)
                # Keep multiple read-only observations in a batch within the
                # same image budget; reject excess pictures with a paired error.
                images = sum(
                    len(resource.get("frames", []))
                    for c in calls
                    if c.result
                    for resource in c.result.get("resources", [])
                    if resource.get("kind") == "observation"
                )
                incoming = sum(
                    len(r.get("frames", []))
                    for r in (result or {}).get("resources", [])
                    if r.get("kind") == "observation"
                )
                if images + incoming > self.settings.vision_max_images:
                    raise AppError("vision_limit", "本批观察图片超出上限，请在下一轮减少采样后重新观察。")
            except AppError as exc:
                if exc.code == "run_control_changed":
                    raise
                result = failure(exc)
            except (ValidationError, ValueError, TypeError, UnicodeError) as exc:
                result = failure(AppError("invalid_tool_arguments", str(exc)[:1800]))
            async with self.db.transaction() as session:
                stored = await session.get(CreativeAgentCall, call.id)
                if result is not None:
                    result["call_id"] = call.call_id
                call.result = result
                call.state = "waiting" if result is None else "completed" if result["ok"] else "failed"
                stored.result = result
                stored.state = "waiting" if result is None else "completed" if result["ok"] else "failed"
                record_event(session, "agent.updated", run.project_id, run.id)
            if result is None:
                return
        async with self.db.transaction() as session:
            current = await session.get(CreativeAgentRun, run.id)
            stored = await session.get(CreativeAgentStep, step.id)
            stored.state = "completed"
            results = (
                await session.scalars(select(CreativeAgentCall).where(CreativeAgentCall.step_id == step.id))
            ).all()
            fingerprints = [self.progress_fingerprint(c) for c in results]
            earlier = (
                await session.scalars(
                    select(CreativeAgentCall).where(
                        CreativeAgentCall.run_id == run.id,
                        CreativeAgentCall.step_id != step.id,
                        CreativeAgentCall.state.in_(("completed", "failed")),
                    )
                )
            ).all()
            old = {self.progress_fingerprint(c) for c in earlier}
            progress = any(
                c.result and c.result.get("ok") and c.name not in ("update_plan", "get_tasks") and fp not in old
                for c, fp in zip(results, fingerprints)
            )
            current.no_progress = 0 if progress else current.no_progress + 1

    async def wake_wait(self, run_id):
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, run_id)
            if run.state != "waiting_task":
                return False
            call = await session.scalar(
                select(CreativeAgentCall).where(
                    CreativeAgentCall.run_id == run.id,
                    CreativeAgentCall.state == "waiting",
                    CreativeAgentCall.name == "wait_for_tasks",
                )
            )
            if not call:
                raise AppError("wait_missing", "持久等待条件缺失，请检查运行记录。", 409)
            args = Tasks.model_validate_json(call.arguments)
            items = await self.tools.task_facts(session, run, args.task_ids)
            if any(item["state"] in ACTIVE_TASKS for item in items):
                return False
            call.result, call.state = success({"items": items}), "completed"
            step = await session.get(CreativeAgentStep, call.step_id)
            step.state = "completed"
            run.state, run.no_progress = "running", 0
            record_event(session, "agent.updated", run.project_id, run.id)
            return True

    async def control(self, run_id, action, version):
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, run_id)
            if not run:
                raise AppError("run_not_found", "自主创作记录不存在。", 404)
            if run.control_version != version:
                raise AppError("revision_conflict", "运行状态已改变，请刷新后重试。", 409)
            if run.state in TERMINAL_RUNS:
                raise AppError("run_ended", "本次创作已结束。", 409)
            if action == "pause":
                if run.state == "cancelling":
                    raise AppError("run_cancelling", "取消正在核对中。", 409)
                run.state = "paused"
            elif action == "resume":
                if run.state not in ("paused", "needs_attention"):
                    raise AppError("run_not_paused", "当前任务无需恢复。", 409)
                last = await session.scalar(
                    select(CreativeAgentStep)
                    .where(CreativeAgentStep.run_id == run_id)
                    .order_by(CreativeAgentStep.sequence.desc())
                    .limit(1)
                )
                if last and last.response_data is None and last.attempts:
                    # Explicit user recovery opens a new bounded request group;
                    # it never resets Run counters or retries existing effects.
                    last.request_data = {
                        **last.request_data,
                        "_manual_retry_groups": [
                            *last.request_data.get("_manual_retry_groups", []),
                            {"attempts": last.attempts, "last_error": last.error, "resumed_at": now()},
                        ],
                    }
                    last.attempts = 0
                waiting = await session.scalar(
                    select(CreativeAgentCall).where(
                        CreativeAgentCall.run_id == run.id, CreativeAgentCall.state == "waiting"
                    )
                )
                run.state = (
                    "waiting_user"
                    if waiting and waiting.name == "ask_user"
                    else "waiting_task"
                    if waiting
                    else "queued"
                )
                if (run.stop_reason or {}).get("code") == "no_progress":
                    run.no_progress = 0
                run.stop_reason = None
            elif action == "cancel":
                run.state = "cancelling"
            else:
                raise AppError("run_control", "不支持的运行控制。")
            run.control_version += 1
            if action in ("pause", "cancel"):
                tick = self.active_ticks.pop(run_id, None)
                if tick is not None:
                    run.active_ms += max(0, round((time.monotonic() - tick) * 1000))
            record_event(session, "agent.updated", run.project_id, run.id)
            result = row_dict(run)
        self.wake.set()
        return result

    async def answer(self, run_id, request: Answer):
        payload = digest({"run_id": run_id, **request.model_dump()})
        async with self.db.transaction() as session:
            old = await session.get(Idempotency, request.idempotency_key)
            if old:
                if old.scope != "agent_answer" or old.payload_hash != payload:
                    raise AppError("idempotency_conflict", "回答提交标识冲突。", 409)
                return old.result
            run = await session.get(CreativeAgentRun, run_id)
            if not run or run.state != "waiting_user" or run.control_version != request.control_version:
                raise AppError("answer_stale", "该任务当前没有等待此回答，或运行状态已改变。", 409)
            call = await session.scalar(
                select(CreativeAgentCall).where(
                    CreativeAgentCall.run_id == run_id,
                    CreativeAgentCall.call_id == request.call_id,
                    CreativeAgentCall.state == "waiting",
                    CreativeAgentCall.name == "ask_user",
                )
            )
            if not call:
                raise AppError("answer_call", "回答不对应当前待澄清的问题。", 409)
            question = json.loads(call.arguments)
            if request.selected_asset_ids and not question.get("allow_assets"):
                raise AppError("answer_assets", "本问题未请求补充素材。", 409)
            from .models import ProjectAsset

            for aid in request.selected_asset_ids:
                membership = await session.get(ProjectAsset, (run.project_id, aid))
                if not membership or membership.removed_at:
                    raise AppError("input_reference", "补充素材不在当前项目。", 409)
            run.scope = {
                **run.scope,
                "selected": list(dict.fromkeys([*run.scope["selected"], *request.selected_asset_ids])),
            }
            # A requested answer is a new explicit user grant, never a reason
            # to reopen the key-assets collection or other prior references.
            if request.selected_asset_ids:
                policy = dict(run.scope.get("reference_policy", {}))
                if policy.get("mode") in ("none", "only"):
                    policy.update(
                        mode="only",
                        only_asset_ids=list(dict.fromkeys([
                            *policy.get("only_asset_ids", []), *request.selected_asset_ids,
                        ])),
                    )
                policy["excluded_asset_ids"] = [
                    aid for aid in policy.get("excluded_asset_ids", []) if aid not in request.selected_asset_ids
                ]
                run.scope = {**run.scope, "reference_policy": policy}
            entry = {
                "call_id": call.call_id,
                "question": question["question"],
                "answer": request.answer,
                "selected_asset_ids": request.selected_asset_ids,
            }
            run.clarifications = [*run.clarifications, entry]
            call.result, call.state = success(entry), "completed"
            step = await session.get(CreativeAgentStep, call.step_id)
            step.state = "completed"
            run.state, run.no_progress = "queued", 0
            session.add(
                Message(
                    project_id=run.project_id,
                    conversation_id=run.conversation_id,
                    role="user",
                    content=request.answer,
                    details={"agent_run_id": run_id, "answer_call_id": call.call_id},
                )
            )
            result = {"run_id": run.id, "state": run.state}
            session.add(
                Idempotency(key=request.idempotency_key, scope="agent_answer", payload_hash=payload, result=result)
            )
            record_event(session, "agent.updated", run.project_id, run.id)
            record_event(session, "message.created", run.project_id, run.id)
        self.wake.set()
        return result

    async def cancel_tasks(self, run_id):
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, run_id)
            if run.state != "cancelling":
                return
            tasks = (await session.scalars(select(Task).where(Task.agent_run_id == run_id))).all()
            ids = [t.id for t in tasks]
            for task in tasks:
                task.cancel_requested = True
                executions = (await session.scalars(select(Execution).where(Execution.task_id == task.id))).all()
                for execution in executions:
                    if execution.execution_state in ("pending", "waiting_external", "blocked"):
                        execution.execution_state, execution.finished_at = "cancelled", now()
        for tid in ids:
            await self.state.worker.aggregate(tid)
        self.state.worker.wake.set()
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, run_id)
            tasks = (await session.scalars(select(Task).where(Task.agent_run_id == run_id))).all()
            if any(t.state in ACTIVE_TASKS for t in tasks):
                return
            if any(t.state == "needs_attention" for t in tasks):
                run.state = "needs_attention"
                run.stop_reason = {"code": "cancel_unknown", "message": "取消存在未知执行或待保存结果，请核对任务。"}
            else:
                run.state, run.finished_at = "cancelled", now()
                calls = (
                    await session.scalars(
                        select(CreativeAgentCall).where(
                            CreativeAgentCall.run_id == run_id, CreativeAgentCall.state.not_in(("completed", "failed"))
                        )
                    )
                ).all()
                for call in calls:
                    task = await session.scalar(select(Task).where(Task.agent_call_id == call.id))
                    call.result = (
                        (await self.tools.creation_result(session, task))
                        if task
                        else failure(AppError("run_cancelled", "用户取消，未继续执行。"))
                    )
                    call.state = "completed" if task else "failed"
            record_event(session, "agent.updated", run.project_id, run.id)
