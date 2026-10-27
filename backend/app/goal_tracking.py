"""Track the requested deliverable independently of internal generation steps."""

from copy import deepcopy

from sqlalchemy import select

from .agent_routing import CreativeBrief
from .errors import AppError
from .models import Asset, ProjectAsset, Setting, uid
from .schemas import PARAMETER_MODELS
from .video_modes import VIDEO_MODES


def goal_key(conversation_id):
    return "video_goal:" + conversation_id


async def store_goal(session, conversation_id, goal):
    row = await session.get(Setting, goal_key(conversation_id))
    if row:
        row.value = deepcopy(goal)
    else:
        session.add(Setting(key=goal_key(conversation_id), value=deepcopy(goal)))


def validate_choice(goal, choice):
    """Accept valid requests from an older open client; do not offer new route choices."""
    if not choice:
        return
    if not goal or goal.get("id") != choice.goal_id or choice.action not in goal.get("available_actions", []):
        raise AppError("goal_changed", "这个视频目标已更新，请直接描述接下来的创作要求。", 409)
    if choice.action == "continue_video" and choice.asset_id not in goal.get("prepared_asset_ids", []):
        raise AppError("goal_input", "此结果不属于当前视频目标。", 409)
    if choice.action != "continue_video" and choice.asset_id is not None:
        raise AppError("goal_input", "此选项不接受其他素材。", 409)


async def finish_video_step(session, task, executions, state):
    frozen = next(((e.configuration.get("prompt_generation") or {}).get("task_goal") for e in executions), None)
    if not frozen or frozen.get("output_kind") != "video":
        return None
    goal = deepcopy(frozen)
    active = await session.get(Setting, goal_key(task.conversation_id))
    is_current = not active or (
        active.value.get("id") == goal["id"]
        and active.value.get("active_task_id", task.id) == task.id
        and active.value.get("plan_id") == goal.get("plan_id")
        and active.value.get("plan_revision") == goal.get("plan_revision")
    )
    goal["available_actions"] = []
    duration = goal.get("video_parameters", {}).get("duration_seconds")
    target = f"{duration} 秒视频" if duration else "视频"
    final = max(executions, key=lambda item: item.candidate_index)
    video_step = final.tool_id in VIDEO_MODES
    assets = []
    if state == "completed":
        assets = list(
            await session.scalars(
                select(Asset.id)
                .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                .where(
                    Asset.execution_id == final.id,
                    Asset.media_type == ("video" if video_step else "image"),
                    Asset.role == "final",
                    ProjectAsset.project_id == task.project_id,
                    ProjectAsset.removed_at.is_(None),
                )
                .order_by(Asset.created_at, Asset.id)
            )
        )
    if state == "completed" and video_step and assets:
        goal.update(state="delivered", current_step="generate_video", video_asset_ids=assets)
        content = f"{target}已生成并保存。请查看视频结果，再告诉我需要怎样调整。"
    elif state == "completed" and not video_step and assets:
        # Old, already-confirmed single-image plans retain their original scope.
        goal.update(state="frame_ready", prepared_asset_ids=assets, source_asset_id=assets[0])
        content = f"旧方案的起始画面已保存，{target}目标仍未完成。继续描述视频要求即可准备视频方案。"
    else:
        goal["state"] = "step_failed" if state != "completed" else "needs_attention"
        content = f"{target}尚未完成，已保留完成的中间素材。请查看任务详情，可从未完成步骤重试。"
    if is_current:
        await store_goal(session, task.conversation_id, goal)
        return content, {"task_goal": goal, "workflow_actions": []}
    return content, {"completed_goal": goal}


class VideoGoalTracker:
    def __init__(self, context, request):
        self.context, self.request = context, request
        self.active = context.get("active_goal")
        self.goal = None
        self.new_id = uid()

    async def bind(self, route, policy):
        intent = route.task_goal
        pending = self.context.get("pending_plan") or {}
        pending_goal = (pending.get("prompt_metadata") or {}).get("task_goal")
        video_tool = route.tool_id in VIDEO_MODES
        continuation = bool(self.request.workflow_choice or (intent and intent.relation == "continue"))
        if intent and intent.output_kind == "image" and video_tool:
            raise ValueError("最终交付物是图片，不能选择视频生成工具")
        if self.request.revise_plan and pending_goal:
            self.goal, continuation = deepcopy(pending_goal), True
        elif continuation:
            if not self.active or self.active.get("output_kind") != "video":
                raise ValueError("没有可继续的视频目标，请明确本轮创作目标")
            self.goal = deepcopy(self.active)
        elif (
            (intent and intent.output_kind == "video")
            or video_tool
            or route.video_continuation
            or any(key in VIDEO_MODES for key in route.creative_brief.deferred_parameters)
            or (
                route.tool_id
                and route.tool_id.startswith("image.")
                and "duration_seconds" in route.creative_brief.requested_parameters
            )
        ):
            quote = (
                intent.request_quote
                if intent
                else (
                    route.creative_brief.shot_intent.intent_quote
                    if route.creative_brief.shot_intent
                    else self.context["user_request"]
                )
            )
            if not await policy.evidence_valid(quote):
                raise ValueError("最终视频目标必须引用当前对话的真实用户需求")
            self.goal = dict(
                id=self.new_id,
                version=2,
                output_kind="video",
                description_zh=intent.description_zh if intent else quote,
                original_request=quote,
                video_parameters={},
                state="planning",
                available_actions=[],
            )
        elif intent and intent.relation == "new":
            self.goal = dict(id=self.new_id, output_kind="image", state="planning", available_actions=[])
        if not self.goal or self.goal["output_kind"] != "video":
            if route.video_continuation:
                raise ValueError("视频续接必须属于最终视频目标")
            return
        if continuation:
            previous = CreativeBrief.model_validate(self.goal.get("creative_brief") or {})
            brief = route.creative_brief
            if not brief.goal.strip():
                brief.goal = previous.goal
            if brief.shot_intent is None and previous.shot_intent:
                brief.shot_intent = previous.shot_intent.model_copy(deep=True)
            if brief.camera_policy == "unspecified":
                brief.camera_policy = previous.camera_policy
            audio = previous.audio_policy.model_copy(deep=True)
            for name in brief.audio_policy.model_fields_set:
                value = getattr(brief.audio_policy, name)
                if value is not None:
                    setattr(audio, name, value)
            brief.audio_policy = audio
            for name in ("must_keep", "requested_changes", "must_not", "literal_texts"):
                current = getattr(brief, name)
                keys = {item.text for item in current}
                current.extend(item for item in getattr(previous, name) if item.text not in keys)
        final_tool = (
            route.tool_id
            if video_tool
            else (route.video_continuation.tool_id if route.video_continuation else self.goal.get("video_tool_id"))
        )
        if not final_tool:
            final_tool = next((key for key in route.creative_brief.deferred_parameters if key in VIDEO_MODES), None)
        parameters = dict(self.goal.get("video_parameters") or {})
        supplied = (
            route.creative_brief.requested_parameters
            if video_tool
            else (route.creative_brief.deferred_parameters.get(final_tool, {}))
        )
        if final_tool:
            model = PARAMETER_MODELS[final_tool]
            parameters = {key: value for key, value in parameters.items() if key in model.model_fields}
            if final_tool in ("video.generate.minimax_t2v", "video.generate.minimax_ref2v"):
                if parameters.get("aspect_ratio") == "source":
                    parameters.pop("aspect_ratio")
            parameters.update(supplied)
            model.model_validate({"prompt": "_", **parameters})
            if video_tool:
                route.creative_brief.requested_parameters = {**parameters, **supplied}
                route.creative_brief.deferred_parameters.pop(final_tool, None)
            elif route.kind == "route":
                route.creative_brief.deferred_parameters[final_tool] = parameters
            self.goal["video_tool_id"] = final_tool
        self.goal.update(video_parameters=parameters, available_actions=[])
        source = next(
            (ref.asset_id for ref in route.input_bindings if ref.slot in ("source_image", "first_frame")), None
        )
        if source:
            self.goal["source_asset_id"] = source
        if not continuation or route.kind == "route":
            self.goal["creative_brief"] = route.creative_brief.model_dump()

    def metadata(self, route=None):
        if not self.goal:
            return {}
        if route and route.kind == "route" and self.goal.get("output_kind") == "video":
            self.goal["creative_brief"] = route.creative_brief.model_dump()
        return {"task_goal": deepcopy(self.goal), "workflow_actions": []}

    def planned(self, route):
        if self.goal and self.goal.get("output_kind") == "video":
            self.goal.pop("active_task_id", None)
            self.goal.update(
                version=2,
                current_step="generate_video" if route.tool_id in VIDEO_MODES else "prepare_first_frame",
                state="awaiting_execution",
                available_actions=[],
                step_count=2 if route.video_continuation else 1,
            )
        return self.metadata(route)
