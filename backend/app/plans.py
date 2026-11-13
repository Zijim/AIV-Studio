import hashlib
import json
from copy import deepcopy

from sqlalchemy import func, select

from .conversations import resolve_conversation, touch_conversation
from .errors import AppError
from .goal_tracking import goal_key, store_goal
from .models import (
    AgentTurn,
    ContextSnapshot,
    Execution,
    Idempotency,
    Message,
    Plan,
    Project,
    ProjectAsset,
    Setting,
    Task,
    record_event,
    row_dict,
    uid,
)
from .prompt_compile import metadata_digest
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
        prompt_metadata=None,
        prompt_mode=None,
        agent_run_id=None,
        agent_call_id=None,
        control_version=None,
        tool_version=None,
    ):
        tool = self.registry.get(request.tool_id, tool_version)
        # Derived values are recomputed, never trusted when a plan is edited.
        values = dict(request.parameters)
        derived = {
            "image.upscale.seedvr2": ("resolution", "max_resolution"),
        }
        computed = ("width", "height", "resolved_frames", "resolved_seconds") if tool["category"] == "video" else ()
        for key in (*derived.get(tool["id"], ()), *computed):
            values.pop(key, None)
        async with self.db.transaction() as session:
            if agent_run_id:
                from .creative_agent_scope import require_running, require_scope_ready, scoped_asset
                from .models import CreativeAgentCall
                run = await require_running(session, agent_run_id, control_version)
                require_scope_ready(run)
                call = await session.get(CreativeAgentCall, agent_call_id)
                if not call or call.run_id != run.id or run.project_id != project_id:
                    raise AppError("agent_owner", "创作调用归属不一致。", 409)
                if call.plan_id:
                    existing = await session.get(Plan, (call.plan_id, 1))
                    if existing:
                        return row_dict(existing)
                for ref in request.inputs:
                    await scoped_asset(session, run, ref.asset_id)
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
            if conversation.mode == "autonomous" and not agent_run_id:
                raise AppError("agent_owner", "自主创作方案必须由所属 Run 创建。", 409)
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
                tool["id"], values, assets, self.settings, request.candidate_count, version=tool["version"]
            )
            if prompt_metadata is None and latest and latest.prompt_metadata:
                if latest.prompt_metadata.get("continuation"):
                    raise AppError(
                        "compound_plan_edit", "请通过对话修改整项视频任务，助手会同步更新起始画面和视频。", 409
                    )
                prompt_metadata = deepcopy(latest.prompt_metadata)
                text_keys = ("prompt", "instruction", "negative_prompt")
                text_changed = any(params.get(k) != latest.parameters.get(k) for k in text_keys)
                inputs_changed = refs != latest.inputs
                timing_changed = params.get("duration_seconds") != latest.parameters.get("duration_seconds")
                if prompt_mode == "manual" or text_changed:
                    prompt_metadata.update(
                        generation_origin="user_edited",
                        validation_status="manual_unchecked",
                        sections=[],
                        prompt_document=None,
                        parent_plan={"id": latest.id, "revision": latest.revision},
                    )
                    prompt_metadata["compiled_prompt_sha256"] = hashlib.sha256(
                        (params.get("prompt") or params.get("instruction") or "").encode()
                    ).hexdigest()
                elif inputs_changed or timing_changed:
                    raise AppError("prompt_stale", "输入或时长已改变，请重新编写提示词，或明确转为手动方案保存。", 409)
            if prompt_mode == "guided" and (
                not prompt_metadata or prompt_metadata.get("validation_status") != "structure_validated"
            ):
                raise AppError("prompt_unvalidated", "当前文本为手动编辑内容，不能标记为专用指引已校验。", 409)
            if prompt_metadata and prompt_metadata.get("task_goal"):
                prompt_metadata = deepcopy(prompt_metadata)
                goal = prompt_metadata["task_goal"]
                if latest and latest.tool_id != tool["id"]:
                    prompt_metadata.pop("task_goal", None)
                    prompt_metadata.pop("workflow_actions", None)
                elif tool["id"].startswith("video."):
                    goal["video_parameters"] = {
                        **goal.get("video_parameters", {}),
                        **{key: params[key] for key in ("duration_seconds", "megapixels", "aspect_ratio")},
                    }
            if prompt_metadata and prompt_metadata.get("continuation"):
                step = prompt_metadata["continuation"]
                followup = self.registry.get(step["tool_id"], step["tool_version"])
                if (
                    not tool["id"].startswith(("image.generate.", "image.edit."))
                    or request.candidate_count != 1
                    or step["tool_id"] != "video.generate.minimax_i2v"
                    or step.get("from_step") != 0
                    or step.get("input_slot") != "source_image"
                    or step["template_hash"] != followup["template_hash"]
                ):
                    raise AppError("pipeline_invalid", "视频任务的内部步骤或工具版本不一致。", 409)
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
            if prompt_metadata and prompt_metadata.get("task_goal"):
                prompt_metadata["task_goal"].update(plan_id=plan_id, plan_revision=revision)
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
            if prompt_metadata:
                snapshot["generation_metadata_hash"] = metadata_digest(prompt_metadata)
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
                prompt_metadata=prompt_metadata,
                based_on_execution_id=request.based_on_execution_id,
                project_revision=context_revision,
                plan_hash=digest(snapshot),
                origin=origin,
                agent_run_id=agent_run_id,
            )
            session.add(plan)
            if agent_run_id:
                call.plan_id = plan.id
            if turn_id:
                turn = await session.get(AgentTurn, turn_id)
                turn.state = "awaiting_execution"
            record_event(session, "plan.created", project_id, plan_id, revision=revision)
            await touch_conversation(session, conversation.id)
            await session.flush()
            result = row_dict(plan)
            result.update(dimensions=dimensions, tool_name=tool["name"], tool_subtitle=tool["subtitle"])
            return result

    async def execute(self, plan_id, request, retry_of=None, *, agent_call_id=None, control_version=None):
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
            if plan.agent_run_id:
                from .creative_agent_policy import reserve_execution
                from .creative_agent_scope import (
                    require_run_background,
                    require_running,
                    require_scope_ready,
                    scoped_asset,
                )
                from .models import CreativeAgentCall
                run = await require_running(session, plan.agent_run_id, control_version)
                require_scope_ready(run)
                call = await session.get(CreativeAgentCall, agent_call_id) if agent_call_id else None
                if not call or call.run_id != run.id or call.plan_id != plan.id:
                    raise AppError("agent_owner", "自主创作方案仅能由所属 Run 提交。", 409)
                await require_run_background(session, run, project)
                for ref in plan.inputs:
                    await scoped_asset(session, run, ref["asset_id"])
            elif conversation.mode == "autonomous":
                raise AppError("agent_owner", "自主创作对话请通过 Agent 发起创作。", 409)
            planning = await session.scalar(
                select(AgentTurn)
                .where(AgentTurn.conversation_id == conversation.id, AgentTurn.state == "planning")
                .limit(1)
            )
            replacing = False
            if planning and planning.context_id:
                snapshot = await session.get(ContextSnapshot, planning.context_id)
                replacing = (snapshot.data.get("revise_plan") or {}).get("plan_id") == plan.id
            if planning and (plan.origin == "agent" or replacing):
                raise AppError("planner_busy", "请等待或放弃当前助手规划，再执行这个方案。", 409)
            active = await session.scalar(
                select(Task.id).where(Task.conversation_id == conversation.id, Task.state.in_(ACTIVE_TASKS)).limit(1)
            )
            if active and plan.origin == "agent":
                raise AppError("await_feedback", "当前对话已有执行中的任务，请等待完成后继续。", 409)
            # Autonomous plans compare the Run's frozen goal/style above instead of
            # a revision-only check. A rename, or an edit reverted before submission,
            # bumps project.revision without changing the background and must not
            # block an already validated autonomous submission.
            if (
                project.revision != plan.project_revision
                and not request.accept_previous_context
                and not plan.agent_run_id
            ):
                raise AppError("context_stale", "项目背景已修改。请更新方案，或明确采用旧背景。", 409)
            if self.registry.get(plan.tool_id, plan.tool_version)["availability"] != "available":
                raise AppError("tool_unavailable", "工具当前不可用，请检查ComfyUI连接和工具状态。", 409)
            continuation = (plan.prompt_metadata or {}).get("continuation")
            if continuation:
                followup = self.registry.get(continuation["tool_id"], continuation["tool_version"])
                if (
                    followup["availability"] != "available"
                    or followup["template_hash"] != continuation["template_hash"]
                ):
                    raise AppError("tool_unavailable", "视频生成步骤当前不可用，请检查工具状态。", 409)
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
                agent_run_id=plan.agent_run_id,
                agent_call_id=agent_call_id if plan.agent_run_id else None,
                queue_seq=seq + 1,
                candidate_count=plan.candidate_count,
                retry_of_task_id=retry_of,
            )
            if plan.agent_run_id:
                await reserve_execution(session, run, call, plan.candidate_count)
            if plan.origin == "retry" and plan.based_on_execution_id:
                source = await session.get(Execution, plan.based_on_execution_id)
                task.retry_of_task_id = source.task_id
            session.add(task)
            await session.flush()
            goal = (plan.prompt_metadata or {}).get("task_goal")
            if goal and goal.get("output_kind") == "video":
                active_goal = await session.get(Setting, goal_key(conversation.id))
                if not active_goal or active_goal.value.get("id") == goal["id"]:
                    await store_goal(
                        session, conversation.id, {**goal, "state": "executing", "active_task_id": task.id}
                    )
            for index, seed in enumerate(plan.seed_list):
                execution_id = uid()
                session.add(
                    Execution(
                        id=execution_id,
                        task_id=task.id,
                        candidate_index=index,
                        client_id=self.client_id,
                        tool_id=plan.tool_id,
                        tool_version=plan.tool_version,
                        seed=seed,
                        parameters={**plan.parameters, "seed": seed},
                        inputs=inputs,
                        agent_quota_state="reserved" if plan.agent_run_id else None,
                        configuration={
                            "comfy_url": self.settings.comfy_url,
                            "timeout_seconds": self.settings.video_timeout_seconds
                            if plan.tool_id.startswith("video.")
                            else self.settings.image_timeout_seconds,
                            "template_hash": self.registry.get(plan.tool_id, plan.tool_version)["template_hash"],
                            "prompt_generation": plan.prompt_metadata,
                            **(
                                {"step_index": 0, "step_label": "准备起始画面", "step_count": 2} if continuation else {}
                            ),
                        },
                    )
                )
                if continuation:
                    session.add(
                        Execution(
                            task_id=task.id,
                            candidate_index=1,
                            client_id=self.client_id,
                            tool_id=continuation["tool_id"],
                            tool_version=continuation["tool_version"],
                            seed=continuation["parameters"]["seed"],
                            parameters=continuation["parameters"],
                            inputs=[],
                            execution_state="blocked",
                            configuration={
                                "comfy_url": self.settings.comfy_url,
                                "timeout_seconds": self.settings.video_timeout_seconds,
                                "template_hash": continuation["template_hash"],
                                "prompt_generation": continuation["prompt_metadata"],
                                "depends_on_execution_id": execution_id,
                                "input_slot": continuation["input_slot"],
                                "step_index": 1,
                                "step_label": "生成视频",
                                "step_count": 2,
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
                    content="已提交视频任务，起始画面准备完成后将自动生成视频。"
                    if continuation
                    else f"已提交：{self.registry.get(plan.tool_id)['name']}，{plan.candidate_count}个候选。",
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
