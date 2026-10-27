import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal

import keyring
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError
from sqlalchemy import and_, or_, select

from .agent_flow import GuidedFlow
from .agent_routing import ROUTING_SYSTEM, RouterResult
from .conversations import default_conversation_id, memory_key, resolve_conversation, touch_conversation
from .errors import AppError
from .goal_tracking import goal_key, store_goal, validate_choice
from .llm_context import SUMMARY_SYSTEM, HistorySummary, TokenCounter
from .models import (
    AgentTurn,
    Asset,
    ContextSnapshot,
    Execution,
    Idempotency,
    LLMCall,
    Message,
    Plan,
    ProjectAsset,
    Setting,
    Task,
    now,
    record_event,
    uid,
)
from .plans import ACTIVE_TASKS, digest, require_project
from .prompt_guides import GuideRegistry
from .schemas import (
    ManualPlan,
)
from .vision import IMAGE_TOKEN_RESERVE, linked_asset_ids, prepare_image, request_audit

PRICES = {
    "currency": "USD",
    "basis": "peak_estimate",
    "input_cache_miss_per_million": "0.30",
    "input_cache_hit_per_million": "0.006",
    "output_per_million": "1.20",
    "checked_on": "2026-10-11",
}
SYSTEM = ROUTING_SYSTEM


def microusd(input_tokens, output_tokens, cache_hits=0):
    value = (
        Decimal(max(0, input_tokens - cache_hits)) * Decimal("0.30")
        + Decimal(cache_hits) * Decimal("0.006")
        + Decimal(output_tokens) * Decimal("1.20")
    )
    return int(value.to_integral_value(rounding=ROUND_CEILING))


class Planner:
    def __init__(self, db, settings, registry, plans, storage):
        self.db, self.settings, self.registry, self.plans, self.storage = db, settings, registry, plans, storage
        self.locks = {}
        self.inflight = {}
        self.counter = TokenCounter()
        self.guides = GuideRegistry(settings.data_dir)

    async def key(self):
        if os.environ.get("DEEPSEEK_API_KEY"):
            return os.environ["DEEPSEEK_API_KEY"].strip()
        try:
            return await asyncio.to_thread(keyring.get_password, "AIVAgent.DeepSeek", "official")
        except keyring.errors.KeyringError:
            return None

    async def credentials(self, value):
        try:
            if value:
                await asyncio.to_thread(keyring.set_password, "AIVAgent.DeepSeek", "official", value.strip())
            else:
                try:
                    await asyncio.to_thread(keyring.delete_password, "AIVAgent.DeepSeek", "official")
                except keyring.errors.PasswordDeleteError:
                    pass
        except keyring.errors.KeyringError as exc:
            raise AppError("credential_store", "Windows凭据保存失败，请改用DEEPSEEK_API_KEY环境变量。", 503) from exc

    async def usage(self):
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        async with self.db.session() as s:
            calls = (await s.scalars(select(LLMCall).where(LLMCall.day == day))).all()
        spent = sum(
            c.estimated_microusd if c.estimated_microusd is not None else c.reserved_microusd
            for c in calls
            if c.state != "rejected"
        )
        return {
            "day": day,
            "estimated_usd": spent / 1e6,
            "daily_budget_usd": self.settings.daily_budget_usd,
            "unknown_calls": sum(
                c.state == "unknown" or (c.state == "completed" and c.estimated_microusd is None) for c in calls
            ),
            "call_count": len(calls),
            "price_snapshot": PRICES,
            "context": {
                "max_input_tokens": self.settings.max_input_tokens,
                "summary_threshold_tokens": self.settings.history_summary_threshold_tokens,
                "max_output_tokens": self.settings.max_output_tokens,
                "tokenizer": "DeepSeek-V4.1-Flash",
            },
            "vision": {
                "max_images": self.settings.vision_max_images,
                "max_edge": self.settings.vision_max_edge,
                "image_token_reserve": IMAGE_TOKEN_RESERVE,
            },
        }

    async def reserve(self, turn_id, request_data, purpose="proposal"):
        tokens = self.counter.request_tokens(request_data)
        if tokens > self.settings.max_input_tokens:
            raise AppError("context_limit", "当前上下文超过输入预算，请缩短消息或减少选择素材。")
        amount = microusd(tokens, request_data["max_output_tokens"])
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        async with self.db.transaction() as s:
            turn = await s.get(AgentTurn, turn_id)
            calls = (await s.scalars(select(LLMCall).where((LLMCall.day == day) | (LLMCall.turn_id == turn_id)))).all()
            purpose_calls = sum(
                c.turn_id == turn_id and (c.request_data.get("_aiv") or {}).get("purpose", "proposal") == purpose
                for c in calls
            )
            limit = {"routing": 1, "composition": 1, "repair": 1, "history_summary": 4, "proposal": 2}.get(purpose, 0)
            if purpose == "continuation_composition":
                snapshot = await s.get(ContextSnapshot, turn.context_id)
                route = (snapshot.data.get("_agent") or {}).get("route") or {}
                limit = int(
                    bool(
                        route.get("video_continuation")
                        and str(route.get("tool_id", "")).startswith(("image.edit.", "image.generate."))
                    )
                )
            if purpose_calls >= limit:
                raise AppError("call_limit", "本轮已达到该阶段调用上限；每个步骤仅编写一次，全流程最多修复一次。")

            def cost(c):
                return (
                    0
                    if c.state == "rejected"
                    else c.estimated_microusd
                    if c.estimated_microusd is not None
                    else c.reserved_microusd
                )

            if amount + sum(cost(c) for c in calls if c.turn_id == turn_id) > int(self.settings.turn_budget_usd * 1e6):
                raise AppError("turn_budget", "本轮模型调用预留超过费用预算。")
            if amount + sum(cost(c) for c in calls if c.day == day) > int(self.settings.daily_budget_usd * 1e6):
                raise AppError("daily_budget", "今日模型费用预算已用完。")
            call = LLMCall(
                id=uid(),
                turn_id=turn_id,
                model=self.settings.llm_model,
                day=day,
                reserved_microusd=amount,
                price_snapshot=PRICES,
                request_data={
                    **request_audit(request_data),
                    "_aiv": {
                        "purpose": purpose,
                        "target_stage": turn.stage,
                        "estimated_input_tokens": tokens,
                        "input_budget": self.counter.request_budget(request_data),
                    },
                },
            )
            s.add(call)
            turn.call_count += 1
            return call.id

    def proposal_request(self, context, repair=None, images=()):
        instructions = SYSTEM + ("\n上次结果未通过校验，请修复：" + repair if repair else "")
        text = json.dumps(
            {k: v for k, v in context.items() if not k.startswith("_")}, ensure_ascii=False, separators=(",", ":")
        )
        content = [{"type": "input_text", "text": text}]
        for image in images:
            content.extend(image.content_parts())
        return {
            "model": self.settings.llm_model,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}] if images else text,
            "reasoning": {"effort": self.settings.reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "creative_proposal",
                    "schema": RouterResult.model_json_schema(),
                }
            },
            "max_output_tokens": self.settings.max_output_tokens,
        }

    async def call(self, turn_id, key, context, repair=None, images=()):
        output = await self.invoke(turn_id, key, self.proposal_request(context, repair, images))
        return RouterResult.model_validate_json(output)

    async def invoke(self, turn_id, key, request, purpose="proposal"):
        call_id = await self.reserve(turn_id, request, purpose)
        try:
            async with AsyncOpenAI(
                api_key=key, base_url=self.settings.llm_url, max_retries=0, timeout=self.settings.llm_timeout
            ) as client:
                response = await client.responses.create(**request)
            result = response.model_dump(mode="json")
            usage = result.get("usage") or {}
            hits = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
            estimated = (
                microusd(usage.get("input_tokens", 0) or 0, usage.get("output_tokens", 0) or 0, hits) if usage else None
            )
            async with self.db.transaction() as s:
                call = await s.get(LLMCall, call_id)
                call.state = "completed"
                call.usage, call.estimated_microusd, call.response_data = usage, estimated, result
                call.response_model, call.provider_request_id = response.model, getattr(response, "_request_id", None)
            if response.status != "completed":
                raise ValueError("模型输出未完整结束，请缩小操作描述或输出长度")
            return response.output_text
        except (APIStatusError, APIConnectionError, APITimeoutError) as exc:
            rejected = isinstance(exc, APIStatusError) and 400 <= exc.status_code < 500
            async with self.db.transaction() as s:
                call = await s.get(LLMCall, call_id)
                call.state = "rejected" if rejected else "unknown"
                call.error = type(exc).__name__ + (
                    f" HTTP {exc.status_code}" if isinstance(exc, APIStatusError) else ""
                )
            code = "llm_auth" if isinstance(exc, APIStatusError) and exc.status_code in (401, 403) else "llm_request"
            raise AppError(code, "DeepSeek调用未完成。请检查密钥、账户余额和网络；本次不会自动重试。", 502) from exc

    @staticmethod
    def history_items(messages):
        items = []
        for message in messages:
            item = {"role": message.role, "content": message.content}
            for field in ("selected_asset_ids", "linked_asset_ids", "vision_images", "visual_observations"):
                value = (message.details or {}).get(field)
                if value:
                    item[field] = value
            items.append(item)
        return items

    async def prepare_vision(self, context, selected_ids, roles):
        assets = {asset["id"]: asset for asset in context["assets"]}
        images = []
        for asset_id in selected_ids:
            if assets[asset_id]["media_type"] != "image":
                continue
            async with self.storage.file_lock:
                asset, blob, path = await self.storage.blob_path(asset_id)
                image = await asyncio.to_thread(
                    prepare_image,
                    path,
                    asset_id=asset_id,
                    blob_id=blob.sha256,
                    name=asset.name,
                    purpose=roles.get(asset_id, "auto"),
                    index=len(images) + 1,
                    max_edge=self.settings.vision_max_edge,
                )
            images.append(image)
        return images

    def visual_records(self, proposal, images):
        supplied = {image.metadata["asset_id"]: image.metadata for image in images}
        observed = [item.asset_id for item in proposal.visual_observations]
        if len(observed) != len(set(observed)) or set(observed) != supplied.keys():
            raise ValueError("visual_observations必须且只能为本轮每张附图各记录一项，不能使用未附图的素材ID")
        if proposal.operation and proposal.operation.tool_id == "video.generate.minimax_i2v":
            source = next((ref.asset_id for ref in proposal.operation.inputs if ref.slot == "source_image"), None)
            role = supplied.get(source, {}).get("purpose", "auto")
            explicit_first = next((i for i, meta in supplied.items() if meta["purpose"] == "first_frame"), None)
            if role not in ("auto", "first_frame") or (explicit_first and source != explicit_first):
                raise ValueError("视频source_image必须遵守用户指定的首帧用途，不能把主体/风格/构图参考直接当成首帧")
        return [
            {
                **supplied[item.asset_id],
                **item.model_dump(),
                "model": self.settings.llm_model,
                "observed_at": now(),
            }
            for item in proposal.visual_observations
        ]

    def summary_request(self, previous_summary, messages):
        return {
            "model": self.settings.llm_model,
            "instructions": SUMMARY_SYSTEM,
            "input": json.dumps(
                {"previous_summary": previous_summary, "messages": self.history_items(messages)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "history_summary",
                    "schema": HistorySummary.model_json_schema(),
                }
            },
            "max_output_tokens": min(4096, self.settings.max_output_tokens),
        }

    async def compact_context(
        self, turn_id, key, project_id, context, messages, memory, pinned_ids, images=(), request_builder=None
    ):
        build = request_builder or (lambda data: self.proposal_request(data, images=images))
        # Keep whole recent messages. Reserve additional room for a proposal repair instruction.
        hard_limit = self.settings.max_input_tokens - 2048
        threshold = min(self.settings.history_summary_threshold_tokens, hard_limit)
        memory = dict(memory or {})
        while self.counter.request_tokens(build(context)) > threshold:
            recent_tokens = 0
            keep = 0
            for message in reversed(messages):
                message_tokens = self.counter.value_tokens(self.history_items([message])[0])
                if keep >= 2 and recent_tokens + message_tokens > self.settings.history_recent_tokens:
                    break
                recent_tokens += message_tokens
                keep += 1
            older_count = len(messages) - keep
            if older_count <= 0:
                break
            summary_limit = min(64 * 1024, hard_limit)
            previous_summary = memory.get("summary", "")
            running_tokens = self.counter.request_tokens(self.summary_request(previous_summary, []))
            chunk = []
            for message in messages[:older_count]:
                extra = self.counter.value_tokens(self.history_items([message])[0])
                if running_tokens + int(extra * 1.05) + 4 > summary_limit:
                    break
                chunk.append(message)
                running_tokens += int(extra * 1.05) + 4
            while chunk and self.counter.request_tokens(self.summary_request(previous_summary, chunk)) > summary_limit:
                chunk.pop()
            if not chunk:
                raise AppError("context_limit", "较早对话中有单条内容超过历史整理预算，请缩短该内容或提高输入预算。")
            try:
                output = await self.invoke(
                    turn_id, key, self.summary_request(previous_summary, chunk), purpose="history_summary"
                )
                summary = HistorySummary.model_validate_json(output).summary
                if not summary.strip():
                    raise ValueError("摘要为空")
            except (ValidationError, ValueError) as exc:
                raise AppError(
                    "history_summary_invalid", "历史整理未完成，原始记录已保留，请重新发送需求。", 502
                ) from exc
            boundary = chunk[-1]
            memory = {
                "summary": summary,
                "through_message_id": boundary.id,
                "through_created_at": boundary.created_at,
                "message_count": memory.get("message_count", 0) + len(chunk),
                "version": memory.get("version", 0) + 1,
                "model": self.settings.llm_model,
                "updated_at": now(),
            }
            messages = messages[len(chunk) :]
            context["history_summary"] = {
                "summary": summary,
                "summarized_message_count": memory["message_count"],
                "through_message_id": boundary.id,
            }
            context["recent_messages"] = self.history_items(messages)
            async with self.db.transaction() as session:
                stored = await session.get(Setting, memory_key(context["conversation"]["id"]))
                if stored:
                    stored.value = memory
                else:
                    session.add(Setting(key=memory_key(context["conversation"]["id"]), value=memory))
                turn = await session.get(AgentTurn, turn_id)
                snapshot = await session.get(ContextSnapshot, turn.context_id)
                snapshot.data = dict(context)
                record_event(session, "turn.context_summarized", project_id, turn_id)
        while self.counter.request_tokens(build(context)) > hard_limit:
            optional_assets = [asset for asset in context["assets"] if not asset["selected"]]
            optional_executions = [
                execution for execution in context["executions"] if execution["id"] not in pinned_ids
            ]
            if optional_assets:
                context["assets"].remove(optional_assets[-1])
            elif optional_executions and len(context["executions"]) > max(1, len(pinned_ids)):
                context["executions"].remove(optional_executions[-1])
            else:
                raise AppError(
                    "context_limit",
                    f"当前需求、已选择素材与近期完整对话超过{self.settings.max_input_tokens // 1024}K输入预算，请减少选择或缩短描述。",
                )
        async with self.db.transaction() as session:
            turn = await session.get(AgentTurn, turn_id)
            snapshot = await session.get(ContextSnapshot, turn.context_id)
            actual_request = build(context)
            budget = {
                "estimated_input_tokens": self.counter.request_tokens(actual_request),
                "input_budget": self.counter.request_budget(actual_request),
                "max_input_tokens": self.settings.max_input_tokens,
                "summary_threshold_tokens": threshold,
                "tokenizer": "DeepSeek-V4.1-Flash",
            }
            context["_budget"] = budget
            context["_stage_budgets"] = {
                **context.get("_stage_budgets", {}),
                actual_request["text"]["format"]["name"]: budget,
            }
            snapshot.data = dict(context)
        return context

    async def prepare(self, project_id, request, on_start=None):
        lock = self.locks.setdefault(request.conversation_id or default_conversation_id(project_id), asyncio.Lock())
        if lock.locked():
            raise AppError("planner_busy", "当前对话正在准备方案。", 409)
        async with lock:
            payload = request.model_dump()
            if request.conversation_id is None:
                payload.pop("conversation_id")  # Preserve legacy idempotency hashes.
            for optional in ("preferred_tool_id", "revise_plan", "workflow_choice"):
                if payload[optional] is None:
                    payload.pop(optional)
            if request.preferred_tool_id:
                self.registry.get(request.preferred_tool_id)
            hashed = digest({"project_id": project_id, **payload})
            async with self.db.session() as s:
                old = await s.get(Idempotency, request.idempotency_key)
                if old:
                    if old.scope != "message" or old.payload_hash != hashed:
                        raise AppError("idempotency_conflict", "提交标识冲突。", 409)
                    return old.result
            key = await self.key()
            if not key:
                raise AppError("llm_not_configured", "请先在设置中保存DeepSeek官方API key。手动工具可以直接使用。", 409)
            linked_ids = linked_asset_ids(request.content, self.settings.port)
            selected_ids = list(dict.fromkeys([*request.selected_asset_ids, *linked_ids]))
            if len(selected_ids) > 30:
                raise AppError("input_reference_limit", "所选素材和消息链接合计每轮最多引用30份素材，请减少引用。")
            async with self.db.transaction() as s:
                project = await require_project(s, project_id, writable=True)
                conversation = await resolve_conversation(s, project_id, request.conversation_id, writable=True)
                cid = conversation.id
                busy = await s.scalar(
                    select(Task).where(Task.conversation_id == cid, Task.state.in_(ACTIVE_TASKS)).limit(1)
                )
                if busy:
                    raise AppError(
                        "await_feedback", "请等待当前对话的任务结束，再根据结果继续；其他对话仍可使用。", 409
                    )
                goal_row = await s.get(Setting, goal_key(cid))
                active_goal = goal_row.value if goal_row else None
                validate_choice(active_goal, request.workflow_choice)
                if request.workflow_choice:
                    choice = request.workflow_choice
                    source = (
                        choice.asset_id if choice.action == "continue_video" else active_goal.get("source_asset_id")
                    )
                    if not source or any(aid != source for aid in selected_ids):
                        raise AppError("goal_input", "此选项需要使用对应目标的图片，请重新选择。", 409)
                    selected_ids = [source]
                rows = (
                    await s.execute(
                        select(Asset, ProjectAsset)
                        .join(ProjectAsset, Asset.id == ProjectAsset.asset_id)
                        .outerjoin(Execution, Asset.execution_id == Execution.id)
                        .outerjoin(Task, Execution.task_id == Task.id)
                        .where(ProjectAsset.project_id == project_id, ProjectAsset.removed_at.is_(None))
                        .where(or_(Asset.execution_id.is_(None), Task.conversation_id == cid))
                        .order_by(Asset.created_at.desc())
                        .limit(60)
                    )
                ).all()
                ids = {a.id for a, m in rows}
                # Explicit selections outside the bounded recent set are added separately.
                for selected in selected_ids:
                    if selected not in ids:
                        membership = await s.get(ProjectAsset, (project_id, selected))
                        asset = await s.get(Asset, selected)
                        if not asset or not membership or membership.removed_at:
                            raise AppError(
                                "input_reference",
                                "链接指向的素材不在当前项目或已移除，请先通过素材库复用或恢复。"
                                if selected in linked_ids
                                else "所选素材不在当前项目。",
                            )
                        rows.append((asset, membership))
                selected_images = {a.id for a, _ in rows if a.id in selected_ids and a.media_type == "image"}
                if not request.image_roles.keys() <= selected_images:
                    raise AppError("vision_role", "只有本轮选中的图片可以设置读图用途。")
                if len(selected_images) > self.settings.vision_max_images:
                    raise AppError(
                        "vision_limit", f"每轮最多读取{self.settings.vision_max_images}张图片，请减少选择后发送。"
                    )
                stored_memory = await s.get(Setting, memory_key(cid))
                memory = dict(stored_memory.value) if stored_memory else {}
                history_query = select(Message).where(Message.project_id == project_id, Message.conversation_id == cid)
                if memory:
                    history_query = history_query.where(
                        or_(
                            Message.created_at > memory["through_created_at"],
                            and_(
                                Message.created_at == memory["through_created_at"],
                                Message.id > memory["through_message_id"],
                            ),
                        )
                    )
                messages = (await s.scalars(history_query.order_by(Message.created_at, Message.id))).all()
                executions = (
                    await s.scalars(
                        select(Execution)
                        .join(Task)
                        .where(Task.project_id == project_id, Task.conversation_id == cid)
                        .order_by(Execution.created_at.desc())
                        .limit(3)
                    )
                ).all()
                pinned = []
                for asset, membership in rows:
                    if asset.id in selected_ids and asset.execution_id:
                        source = await s.get(Execution, asset.execution_id)
                        owner = await s.get(Task, source.task_id)
                        if owner.project_id == project_id and source.id not in {e.id for e in pinned}:
                            pinned.append(source)
                executions = pinned + [e for e in executions if e.id not in {p.id for p in pinned}]
                pending = await s.scalar(
                    select(Plan)
                    .where(Plan.project_id == project_id, Plan.conversation_id == cid, Plan.state == "draft")
                    .order_by(Plan.created_at.desc())
                    .limit(1)
                )
                if request.revise_plan:
                    target = request.revise_plan
                    pending = await s.scalar(
                        select(Plan).where(Plan.id == target.plan_id).order_by(Plan.revision.desc()).limit(1)
                    )
                    if not pending or pending.project_id != project_id or pending.conversation_id != cid:
                        raise AppError("plan_not_found", "所指定方案不属于当前对话。", 404)
                    if pending.revision != target.revision or pending.state != "draft":
                        raise AppError("revision_conflict", "方案已更新或执行，请刷新后重试。", 409)
                    if request.preferred_tool_id and request.preferred_tool_id != pending.tool_id:
                        raise AppError("tool_conflict", "限定工具与要修改的方案不一致。", 409)
                    if target.rewrite_only:
                        tool = self.registry.get(pending.tool_id)
                        if tool["availability"] != "available":
                            raise AppError("tool_unavailable", "此工具当前不可用，请连接ComfyUI后重新编写提示词。", 409)
                        self.guides.get(pending.tool_id, tool["version"])
                    for ref in pending.inputs:
                        if ref["asset_id"] not in {a.id for a, _ in rows}:
                            asset = await s.get(Asset, ref["asset_id"])
                            membership = await s.get(ProjectAsset, (project_id, ref["asset_id"]))
                            if not asset or not membership or membership.removed_at:
                                raise AppError("input_reference", "原方案的输入已移除，请重新选择。", 409)
                            rows.append((asset, membership))
                assets = []
                required_ids = (
                    set(selected_ids) | {ref["asset_id"] for ref in pending.inputs} if pending else set(selected_ids)
                )
                for a, m in rows:
                    if len(assets) >= 12 and a.id not in required_ids:
                        continue
                    view = await self.storage.asset_view(s, a, m)
                    assets.append(
                        {
                            "id": a.id,
                            "name": a.name,
                            "description": a.description[:500],
                            "description_source": "generation_prompt_or_import_metadata",
                            "note": m.note[:500],
                            "media_type": a.media_type,
                            "media_info": a.media_info,
                            "presence": view["presence"],
                            "selected": a.id in selected_ids,
                            "reference_source": "selection"
                            if a.id in request.selected_asset_ids
                            else "message_link"
                            if a.id in linked_ids
                            else "metadata_only",
                            "execution_id": a.execution_id,
                        }
                    )
                context = {
                    "conversation": {k: getattr(conversation, k) for k in ("id", "title", "revision")},
                    "history_summary": {
                        "summary": memory["summary"],
                        "summarized_message_count": memory["message_count"],
                        "through_message_id": memory["through_message_id"],
                    }
                    if memory
                    else None,
                    "pending_plan": {
                        k: getattr(pending, k)
                        for k in (
                            "id",
                            "revision",
                            "turn_id",
                            "tool_id",
                            "inputs",
                            "parameters",
                            "candidate_count",
                            "prompt_metadata",
                        )
                    }
                    if pending
                    else None,
                    "project": {k: getattr(project, k) for k in ("id", "name", "goal", "style", "revision")},
                    "assets": assets,
                    "vision_images": [],
                    "recent_messages": self.history_items(messages),
                    "executions": [
                        {
                            "id": e.id,
                            "tool_id": e.tool_id,
                            "parameters": e.parameters,
                            "inputs": e.inputs,
                            "state": e.execution_state,
                            "pinned": e.id in {source.id for source in pinned},
                        }
                        for e in executions
                    ],
                    "tools": self.guides.cards(self.registry, self.settings),
                    "preferred_tool_id": request.preferred_tool_id,
                    "revise_plan": request.revise_plan.model_dump() if request.revise_plan else None,
                    "active_goal": active_goal,
                    "workflow_choice": request.workflow_choice.model_dump() if request.workflow_choice else None,
                    "user_request": request.content,
                }

                if conversation.auto_title and not messages:
                    title = re.sub(r"https?://\S+|/api/v1/assets/\S+", "", request.content).strip()
                    conversation.title = " ".join(title.split())[:40] or "图片讨论"
                    conversation.auto_title = False
                    conversation.revision += 1
                    context["conversation"] = {k: getattr(conversation, k) for k in ("id", "title", "revision")}
                snapshot = ContextSnapshot(id=uid(), project_id=project_id, conversation_id=cid, data=context)
                s.add(snapshot)
                await s.flush()
                turn = AgentTurn(id=uid(), project_id=project_id, conversation_id=cid, context_id=snapshot.id)
                s.add(turn)
                await s.flush()
                user_message = Message(
                    id=uid(),
                    project_id=project_id,
                    conversation_id=cid,
                    turn_id=turn.id,
                    role="user",
                    content=request.content,
                    details={"selected_asset_ids": request.selected_asset_ids, "linked_asset_ids": linked_ids},
                )
                s.add(user_message)
                result = {"turn_id": turn.id, "conversation_id": cid, "state": "planning"}
                s.add(Idempotency(key=request.idempotency_key, payload_hash=hashed, scope="message", result=result))
                record_event(s, "message.created", project_id, turn.id)
                await touch_conversation(s, cid)
            self.inflight[turn.id] = asyncio.current_task()
            if on_start:
                on_start(result)
            try:
                images = await self.prepare_vision(context, selected_ids, request.image_roles)
                context["vision_images"] = [image.metadata for image in images]
                async with self.db.transaction() as s:
                    stored_message = await s.get(Message, user_message.id)
                    stored_message.details = {**stored_message.details, "vision_images": context["vision_images"]}
                    stored_snapshot = await s.get(ContextSnapshot, snapshot.id)
                    stored_snapshot.data = dict(context)
                    if images:
                        record_event(s, "turn.vision_prepared", project_id, turn.id, image_count=len(images))
                outcome = await GuidedFlow(self, turn.id, key, context, request, images, user_message.id).run()
                proposal, observations = outcome, outcome.observations
                plan = None
                if outcome.operation:
                    plan = await self.plans.create(
                        project_id,
                        ManualPlan(
                            **outcome.operation,
                            conversation_id=cid,
                            change_summary=outcome.change_summary,
                            based_on_execution_id=outcome.based_on_execution_id,
                        ),
                        turn_id=turn.id,
                        context_id=snapshot.id,
                        origin="agent",
                        plan_id=pending.id if outcome.based_on_plan_id else None,
                        expected_revision=pending.revision if outcome.based_on_plan_id else None,
                        replace_owner=bool(outcome.based_on_plan_id),
                        prompt_metadata=outcome.metadata,
                    )
                    outcome.metadata = plan.get("prompt_metadata") or outcome.metadata
                async with self.db.transaction() as s:
                    current = await s.get(AgentTurn, turn.id)
                    outcome_meta = outcome.metadata or {}
                    if outcome_meta.get("task_goal"):
                        await store_goal(s, cid, outcome_meta["task_goal"])
                    current.state = (
                        "awaiting_execution"
                        if plan
                        else "needs_clarification"
                        if proposal.kind == "clarify"
                        else "completed"
                    )
                    if plan:
                        # New agent intent replaces unexecuted earlier agent proposals.
                        old_plans = (
                            await s.scalars(
                                select(Plan).where(
                                    Plan.project_id == project_id,
                                    Plan.conversation_id == cid,
                                    Plan.origin == "agent",
                                    Plan.state == "draft",
                                    Plan.id != plan["id"],
                                )
                            )
                        ).all()
                        for old_plan in old_plans:
                            old_plan.state = "superseded"
                    s.add(
                        Message(
                            project_id=project_id,
                            conversation_id=cid,
                            turn_id=turn.id,
                            role="assistant",
                            content=proposal.message,
                            plan_id=plan["id"] if plan else None,
                            details={
                                "visual_observations": observations,
                                "planning_version": "guided_v2",
                                "task_goal": outcome_meta.get("task_goal"),
                                "workflow_actions": outcome_meta.get("workflow_actions", []),
                            },
                        )
                    )
                    result = {"turn_id": turn.id, "conversation_id": cid, "state": current.state, "plan": plan}
                    idem = await s.get(Idempotency, request.idempotency_key)
                    idem.result = result
                    record_event(s, "message.created", project_id, turn.id)
                    await touch_conversation(s, cid)
                return result
            except asyncio.CancelledError:
                async with self.db.transaction() as s:
                    current = await s.get(AgentTurn, turn.id)
                    current.state = "abandoned"
                    current.error = {"code": "planning_interrupted", "message": "本轮方案准备已中止，未执行生成。"}
                    for call in (
                        await s.scalars(select(LLMCall).where(LLMCall.turn_id == turn.id, LLMCall.state == "reserved"))
                    ).all():
                        call.state = "unknown"
                    idem = await s.get(Idempotency, request.idempotency_key)
                    idem.result = {"turn_id": turn.id, "state": "abandoned"}
                    s.add(
                        Message(
                            project_id=project_id,
                            conversation_id=cid,
                            turn_id=turn.id,
                            role="assistant",
                            content=current.error["message"],
                        )
                    )
                    record_event(s, "turn.updated", project_id, turn.id)
                raise
            except Exception as exc:
                async with self.db.transaction() as s:
                    current = await s.get(AgentTurn, turn.id)
                    current.state = "failed"
                    current.error = {
                        "code": getattr(exc, "code", "planner_failed"),
                        "message": getattr(exc, "message", "方案准备失败，请查看诊断信息。"),
                    }
                    if getattr(exc, "details", None):
                        current.error["details"] = exc.details
                    s.add(
                        Message(
                            project_id=project_id,
                            conversation_id=cid,
                            turn_id=turn.id,
                            role="assistant",
                            content=current.error["message"],
                        )
                    )
                    idem = await s.get(Idempotency, request.idempotency_key)
                    idem.result = {"turn_id": turn.id, "state": "failed", "error": current.error}
                    record_event(s, "message.created", project_id, turn.id)
                raise
            finally:
                self.inflight.pop(turn.id, None)
