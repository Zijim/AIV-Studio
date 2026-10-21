import hashlib
import json

from sqlalchemy import func, select

from .conversations import resolve_conversation, touch_conversation
from .errors import AppError
from .models import (
    AgentTurn,
    ContextSnapshot,
    Execution,
    Idempotency,
    Message,
    Plan,
    Project,
    ProjectAsset,
    Task,
    record_event,
    row_dict,
    uid,
)
from .schemas import ManualPlan

ACTIVE_TASKS = ("queued", "running", "saving")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


async def require_project(session, project_id, writable=False):
    project = await session.get(Project, project_id)
    if not project or project.removed_at:
        raise AppError("project_not_found", "项目不存在。", 404)
    if writable and project.archived_at:
        raise AppError("project_archived", "请先解除归档再进行创作。", 409)
    return project


class PlanService:
    def __init__(self, db, registry, storage, settings):
        self.db, self.registry, self.storage, self.settings = db, registry, storage, settings
        self.client_id = uid()

    async def create(
        self,
        project_id,
        request: ManualPlan,
        *,
        plan_id=None,
        expected_revision=None,
        turn_id=None,
        context_id=None,
        origin="manual",
        preserve_seed=False,
        replace_owner=False,
        conversation_id=None,
    ):
        tool = self.registry.get(request.tool_id)
        # Derived values are recomputed, never trusted when a plan is edited.
        values = dict(request.parameters)
        derived = {
            "image.upscale.seedvr2": ("resolution", "max_resolution"),
            "video.generate.minimax_i2v": ("width", "height", "resolved_frames", "resolved_seconds"),
        }
        for key in derived.get(tool["id"], ()):
            values.pop(key, None)
        async with self.db.transaction() as session:
            project = await require_project(session, project_id, writable=True)
            latest = None
            if plan_id:
                latest = await session.scalar(
                    select(Plan).where(Plan.id == plan_id).order_by(Plan.revision.desc()).limit(1)
                )
                if not latest or latest.project_id != project_id:
                    raise AppError("plan_not_found", "方案不存在。", 404)
                if request.conversation_id and request.conversation_id != latest.conversation_id:
                    raise AppError("conversation_mismatch", "方案不能转移到其他对话。", 409)
            conversation = await resolve_conversation(
                session,
                project_id,
                conversation_id or request.conversation_id or (latest.conversation_id if latest else None),
                writable=True,
            )
            if latest and latest.conversation_id != conversation.id:
                raise AppError("conversation_mismatch", "方案不能转移到其他对话。", 409)
            if turn_id:
                owner = await session.get(AgentTurn, turn_id)
                if not owner or owner.project_id != project_id or owner.conversation_id != conversation.id:
                    raise AppError("conversation_mismatch", "方案回合不属于当前对话。", 409)
            if context_id:
                context = await session.get(ContextSnapshot, context_id)
                if not context or context.project_id != project_id or context.conversation_id != conversation.id:
                    raise AppError("conversation_mismatch", "方案上下文不属于当前对话。", 409)
            if request.based_on_execution_id:
                source = await session.get(Execution, request.based_on_execution_id)
                source_task = await session.get(Task, source.task_id) if source else None
                if not source_task or source_task.project_id != project_id:
                    raise AppError("source_execution", "来源执行不属于当前项目。")
            refs = [r.model_dump() for r in request.inputs]
            if len({r["slot"] for r in refs}) != len(refs):
                raise AppError("input_slots", "输入槽位不能重复。")
            assets = {}
            for item in refs:
                membership = await session.get(ProjectAsset, (project_id, item["asset_id"]))
                if not membership or membership.removed_at:
                    raise AppError("input_reference", "输入素材不在当前项目，或已被移除。请明确复用或重新选择。", 409)
                asset, blob, _ = await self.storage.blob_path(item["asset_id"], session)
                assets[item["slot"]] = row_dict(asset)
            params, seeds, dimensions = self.registry.normalize(
                tool["id"], values, assets, self.settings, request.candidate_count
            )
            revision = 1
            if plan_id:
                if latest.revision != expected_revision:
                    raise AppError("revision_conflict", "方案已更新，请刷新后再修改。", 409)
                if latest.state != "draft":
                    raise AppError("plan_consumed", "该方案已执行或放弃，请创建新操作。", 409)
                latest.state = "superseded"
                revision = latest.revision + 1
                if replace_owner:
                    if latest.turn_id:
                        previous_turn = await session.get(AgentTurn, latest.turn_id)
                        previous_turn.state = "superseded"
                else:
                    turn_id, context_id, origin = latest.turn_id, latest.context_id, latest.origin
            plan_id = plan_id or uid()
            context_revision = project.revision
            if context_id and (not expected_revision or replace_owner):
                context = await session.get(ContextSnapshot, context_id)
                context_revision = context.data["project"]["revision"]
            snapshot = dict(
                conversation_id=conversation.id,
                tool_id=tool["id"],
                tool_version=tool["version"],
                inputs=refs,
                parameters=params,
                seed_list=seeds,
                candidate_count=request.candidate_count,
                project_revision=context_revision,
            )
            plan = Plan(
                id=plan_id,
                revision=revision,
                project_id=project_id,
                conversation_id=conversation.id,
                turn_id=turn_id,
                context_id=context_id,
                tool_id=tool["id"],
                tool_version=tool["version"],
                inputs=refs,
                parameters=params,
                seed_list=seeds,
                candidate_count=request.candidate_count,
                change_summary=request.change_summary,
                based_on_execution_id=request.based_on_execution_id,
                project_revision=context_revision,
                plan_hash=digest(snapshot),
                origin=origin,
            )
            session.add(plan)
            if turn_id:
                turn = await session.get(AgentTurn, turn_id)
                turn.state = "awaiting_execution"
            record_event(session, "plan.created", project_id, plan_id, revision=revision)
            await touch_conversation(session, conversation.id)
            await session.flush()
            result = row_dict(plan)
            result.update(dimensions=dimensions, tool_name=tool["name"], tool_subtitle=tool["subtitle"])
            return result

    async def execute(self, plan_id, request, retry_of=None):
        payload_hash = digest(
            dict(
                plan_id=plan_id,
                revision=request.revision,
                hash=request.plan_hash,
                accept_previous=request.accept_previous_context,
            )
        )
        async with self.db.transaction() as session:
            old = await session.get(Idempotency, request.idempotency_key)
            if old:
                if old.scope != "execute" or old.payload_hash != payload_hash:
                    raise AppError("idempotency_conflict", "此提交标识已经用于另一请求。", 409)
                return old.result
            plan = await session.get(Plan, (plan_id, request.revision))
            if not plan:
                raise AppError("plan_not_found", "方案不存在。", 404)
            latest = await session.scalar(select(func.max(Plan.revision)).where(Plan.id == plan_id))
            if latest != request.revision or plan.state != "draft" or plan.plan_hash != request.plan_hash:
                raise AppError("plan_changed", "方案已改变或执行，请刷新确认。", 409)
            project = await require_project(session, plan.project_id, writable=True)
            conversation = await resolve_conversation(session, plan.project_id, plan.conversation_id, writable=True)
            planning = await session.scalar(
                select(AgentTurn)
                .where(AgentTurn.conversation_id == conversation.id, AgentTurn.state == "planning")
                .limit(1)
            )
            if planning and plan.origin == "agent":
                raise AppError("planner_busy", "请等待或放弃当前助手规划，再执行这个方案。", 409)
            active = await session.scalar(
                select(Task.id).where(Task.conversation_id == conversation.id, Task.state.in_(ACTIVE_TASKS)).limit(1)
            )
            if active and plan.origin == "agent":
                raise AppError("await_feedback", "当前对话已有执行中的任务，请等待完成后继续。", 409)
            if project.revision != plan.project_revision and not request.accept_previous_context:
                raise AppError("context_stale", "项目背景已修改。请更新方案，或明确采用旧背景。", 409)
            if self.registry.get(plan.tool_id)["availability"] != "available":
                raise AppError("tool_unavailable", "工具当前不可用，请检查ComfyUI连接和工具状态。", 409)
            self.storage.check_space()
            count = await session.scalar(select(func.count()).select_from(Task).where(Task.state.in_(ACTIVE_TASKS)))
            if count >= self.settings.max_outstanding_tasks:
                raise AppError("queue_full", "创作队列已满，请等待或取消现有任务。", 409)
            inputs = []
            for item in plan.inputs:
                membership = await session.get(ProjectAsset, (plan.project_id, item["asset_id"]))
                if not membership or membership.removed_at:
                    raise AppError("input_removed", "确认后的输入素材已移除，请重新准备方案。", 409)
                _, blob, path = await self.storage.blob_path(item["asset_id"], session)
                inputs.append({**item, "blob_id": blob.sha256})
            seq = (await session.scalar(select(func.max(Task.queue_seq)))) or 0
            task = Task(
                id=uid(),
                project_id=plan.project_id,
                conversation_id=conversation.id,
                plan_id=plan.id,
                plan_revision=plan.revision,
                turn_id=plan.turn_id,
                origin=plan.origin,
                queue_seq=seq + 1,
                candidate_count=plan.candidate_count,
                retry_of_task_id=retry_of,
            )
            if plan.origin == "retry" and plan.based_on_execution_id:
                source = await session.get(Execution, plan.based_on_execution_id)
                task.retry_of_task_id = source.task_id
            session.add(task)
            await session.flush()
            for index, seed in enumerate(plan.seed_list):
                session.add(
                    Execution(
                        task_id=task.id,
                        candidate_index=index,
                        client_id=self.client_id,
                        tool_id=plan.tool_id,
                        tool_version=plan.tool_version,
                        seed=seed,
                        parameters={**plan.parameters, "seed": seed},
                        inputs=inputs,
                        configuration={
                            "comfy_url": self.settings.comfy_url,
                            "timeout_seconds": self.settings.video_timeout_seconds
                            if plan.tool_id.startswith("video.")
                            else self.settings.image_timeout_seconds,
                            "template_hash": self.registry.get(plan.tool_id)["template_hash"],
                        },
                    )
                )
            plan.state = "consumed"
            if plan.turn_id:
                turn = await session.get(AgentTurn, plan.turn_id)
                turn.state = "executing"
            session.add(
                Message(
                    project_id=plan.project_id,
                    conversation_id=conversation.id,
                    turn_id=plan.turn_id,
                    role="system",
                    content=f"已提交：{self.registry.get(plan.tool_id)['name']}，{plan.candidate_count}个候选。",
                    task_id=task.id,
                )
            )
            result = dict(task_id=task.id, state="queued")
            session.add(
                Idempotency(key=request.idempotency_key, scope="execute", payload_hash=payload_hash, result=result)
            )
            record_event(session, "task.updated", plan.project_id, task.id, state="queued")
            await touch_conversation(session, conversation.id)
            return result
