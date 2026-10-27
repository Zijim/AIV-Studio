"""Keep a video deliverable separate from its optional, user-chosen image step."""

from copy import deepcopy

from sqlalchemy import select

from .agent_routing import CreativeBrief
from .errors import AppError
from .models import Asset, ProjectAsset, Setting, uid
from .schemas import VideoParams

VIDEO_TOOL = "video.generate.minimax_i2v"


def goal_key(conversation_id):
    return "video_goal:" + conversation_id


async def store_goal(session, conversation_id, goal):
    row = await session.get(Setting, goal_key(conversation_id))
    if row:
        row.value = deepcopy(goal)
    else:
        session.add(Setting(key=goal_key(conversation_id), value=deepcopy(goal)))


def validate_choice(goal, choice):
    if not choice:
        return
    if not goal or goal.get("id") != choice.goal_id or choice.action not in goal.get("available_actions", []):
        raise AppError("goal_changed", "这个视频目标或可选步骤已更新，请使用最新消息中的选项。", 409)
    if choice.action == "continue_video":
        if choice.asset_id not in goal.get("prepared_asset_ids", []):
            raise AppError("goal_input", "请选择这个视频目标生成的首帧结果。", 409)
    elif choice.asset_id is not None:
        raise AppError("goal_input", "此选项不接受其他素材，请使用当前目标的输入。", 409)


def workflow_actions(goal):
    if not goal:
        return []
    common = {"goal_id": goal["id"]}
    actions = []
    if "direct_video" in goal.get("available_actions", []):
        actions.append({**common, "action": "direct_video", "label": "直接尝试视频"})
    if "prepare_first_frame" in goal.get("available_actions", []):
        actions.append({**common, "action": "prepare_first_frame", "label": "先准备首帧"})
    if "continue_video" in goal.get("available_actions", []):
        ids = goal.get("prepared_asset_ids", [])
        for i, asset_id in enumerate(ids):
            label = "用这张首帧准备视频" if len(ids) == 1 else f"用首帧 {i + 1} 准备视频"
            actions.append({**common, "action": "continue_video", "asset_id": asset_id, "label": label})
    return actions


async def finish_video_step(session, task, executions, state):
    frozen = next(((e.configuration.get("prompt_generation") or {}).get("task_goal") for e in executions), None)
    if not frozen or frozen.get("output_kind") != "video":
        return None
    goal = deepcopy(frozen)
    active = await session.get(Setting, goal_key(task.conversation_id))
    is_current = not active or (
        active.value.get("id") == goal["id"]
        and active.value.get("current_step") == goal.get("current_step")
        and active.value.get("active_task_id", task.id) == task.id
    )
    goal["available_actions"] = []
    step = goal.get("current_step")
    duration = goal.get("video_parameters", {}).get("duration_seconds")
    target = f"{duration} 秒视频" if duration else "视频"
    if state == "completed":
        media_type = "image" if step == "prepare_first_frame" else "video"
        assets = list(
            await session.scalars(
                select(Asset.id)
                .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                .where(
                    Asset.execution_id.in_([e.id for e in executions]),
                    Asset.media_type == media_type,
                    Asset.role != "preview",
                    ProjectAsset.project_id == task.project_id,
                    ProjectAsset.removed_at.is_(None),
                )
                .order_by(Asset.created_at, Asset.id)
            )
        )
        if step == "prepare_first_frame" and assets:
            goal.update(state="frame_ready", prepared_asset_ids=assets, available_actions=["continue_video"])
            content = (
                f"首帧已生成，最终目标的{target}尚未生成。请查看首帧，再选择结果继续准备视频方案；视频仍需确认后执行。"
            )
        elif step == "generate_video" and assets:
            goal.update(state="delivered", video_asset_ids=assets)
            content = f"{target}已生成并保存。请查看视频结果，再告诉我需要怎样调整。"
        else:
            goal["state"] = "needs_attention"
            label = "首帧图片" if media_type == "image" else "视频"
            content = f"当前步骤没有保存可用的{label}，{target}目标仍未完成。请查看任务详情。"
    else:
        goal["state"] = "step_failed"
        content = f"当前{'首帧准备' if step == 'prepare_first_frame' else '视频生成'}步骤未完成，最终{target}目标仍未完成。请查看任务详情处理或重试。"
    if is_current:
        await store_goal(session, task.conversation_id, goal)
    else:
        goal["available_actions"] = []
        # A recovery callback for an older goal must not hide the current goal's UI choices.
        return content, {"completed_goal": goal}
    return content, {"task_goal": goal, "workflow_actions": workflow_actions(goal)}


class VideoGoalTracker:
    def __init__(self, context, request):
        self.context, self.request = context, request
        self.active = context.get("active_goal")
        self.goal = None
        self.new_id = uid()
        self.direct = False
        self.preparation_approved = False

    async def bind(self, route, policy):
        intent = route.task_goal
        choice = self.request.workflow_choice
        if route.kind == "route" and intent and intent.output_kind == "image" and route.tool_id == VIDEO_TOOL:
            raise ValueError("最终交付物是图片，不能选择视频生成工具")
        continuation = bool(
            choice
            or (intent and intent.relation == "continue")
            or (
                not intent
                and self.active
                and self.active.get("state") == "awaiting_choice"
                and (route.preparation_consent_quote or route.direct_video_consent_quote)
            )
        )
        pending = self.context.get("pending_plan") or {}
        pending_goal = (pending.get("prompt_metadata") or {}).get("task_goal")
        if continuation and not self.active and not pending_goal:
            raise ValueError("没有可继续的视频目标，请说明新的生成要求")
        if self.request.revise_plan and pending_goal:
            self.goal = deepcopy(pending_goal)
            continuation = True
        elif continuation and self.active and self.active.get("output_kind") == "video":
            self.goal = deepcopy(self.active)
        elif (intent and intent.output_kind == "video") or (
            not intent
            and (
                route.tool_id == VIDEO_TOOL
                or VIDEO_TOOL in route.creative_brief.deferred_parameters
                or (
                    route.tool_id
                    and route.tool_id.startswith("image.")
                    and "duration_seconds" in route.creative_brief.requested_parameters
                )
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
            self.goal = {
                "id": self.new_id,
                "output_kind": "video",
                "description_zh": intent.description_zh if intent else quote,
                "original_request": quote,
                "video_parameters": {},
                "state": "planning",
                "available_actions": [],
            }
        elif intent and intent.relation == "new" and intent.output_kind == "image":
            self.goal = {"id": self.new_id, "output_kind": "image", "state": "planning", "available_actions": []}
        else:
            return
        if continuation and not self.goal:
            raise ValueError("没有可继续的视频目标，请说明新的生成要求")
        if self.goal["output_kind"] != "video":
            return
        for name in ("preparation_consent_quote", "direct_video_consent_quote"):
            quote = getattr(route, name)
            if quote and (not quote.strip() or quote not in self.context["user_request"]):
                raise ValueError("首帧准备或直接尝试的同意必须引用本轮用户原文")
        self.preparation_approved = bool(
            route.preparation_consent_quote
            or (choice and choice.action == "prepare_first_frame")
            or (self.request.revise_plan and pending_goal and pending_goal.get("current_step") == "prepare_first_frame")
        )
        self.direct = bool(route.direct_video_consent_quote or (choice and choice.action == "direct_video"))
        if self.request.revise_plan and self.request.revise_plan.rewrite_only and pending_goal:
            self.direct = self.direct or pending_goal.get("direct_video_approved", False)
        if choice and route.kind == "route":
            if choice.action in ("direct_video", "continue_video") and route.tool_id != VIDEO_TOOL:
                raise ValueError("用户已选择准备视频，必须使用视频工具")
            if choice.action == "prepare_first_frame" and not route.tool_id.startswith(
                ("image.edit", "image.generate")
            ):
                raise ValueError("用户已选择先准备首帧，本轮必须准备图片方案")
            expected = choice.asset_id if choice.action == "continue_video" else self.goal.get("source_asset_id")
            if expected and not any(ref.asset_id == expected for ref in route.input_bindings):
                raise ValueError("必须使用用户为此步骤选择的输入图片")
        if continuation:
            previous = self.goal.get("creative_brief") or {}
            original = CreativeBrief.model_validate(previous)
            if not route.creative_brief.goal.strip():
                route.creative_brief.goal = original.goal
            if route.creative_brief.shot_intent is None:
                route.creative_brief.shot_intent = (
                    original.shot_intent.model_copy(deep=True) if original.shot_intent else None
                )
            if route.creative_brief.camera_policy == "unspecified":
                route.creative_brief.camera_policy = original.camera_policy
            audio = original.audio_policy.model_copy(deep=True)
            for name in route.creative_brief.audio_policy.model_fields_set:
                value = getattr(route.creative_brief.audio_policy, name)
                if value is not None:
                    setattr(audio, name, value)
            route.creative_brief.audio_policy = audio
            for name in ("must_keep", "requested_changes", "must_not", "literal_texts"):
                current = getattr(route.creative_brief, name)
                keys = {item.text for item in current}
                current.extend(item for item in getattr(original, name) if item.text not in keys)
            if choice:
                route.creative_brief.goal = original.goal or self.goal["original_request"]
                route.creative_brief.camera_policy = original.camera_policy
                route.creative_brief.audio_policy = original.audio_policy.model_copy(deep=True)
                if original.shot_intent:
                    # The actual starting frame/scene may change, but a button cannot change the requested action.
                    shot = route.creative_brief.shot_intent or original.shot_intent.model_copy(deep=True)
                    for name in ("mode", "intent_quote", "hold_evidence", "progression_zh", "end_state_zh"):
                        setattr(shot, name, getattr(original.shot_intent, name))
                    route.creative_brief.shot_intent = shot
        parameters = dict(self.goal.get("video_parameters") or {})
        supplied = (
            route.creative_brief.requested_parameters
            if route.tool_id in (None, VIDEO_TOOL)
            else route.creative_brief.deferred_parameters.get(VIDEO_TOOL, {})
        )
        if choice and any(key in supplied and supplied[key] != value for key, value in parameters.items()):
            raise ValueError("继续原目标时不能改变已记录的视频时长、尺寸或种子")
        parameters.update(supplied)
        if route.tool_id and route.tool_id.startswith("image."):
            if "duration_seconds" in route.creative_brief.requested_parameters:
                parameters["duration_seconds"] = route.creative_brief.requested_parameters["duration_seconds"]
        VideoParams.model_validate({"prompt": "_", **parameters})
        self.goal["video_parameters"] = parameters
        if route.tool_id == VIDEO_TOOL:
            route.creative_brief.requested_parameters = {**parameters, **route.creative_brief.requested_parameters}
            route.creative_brief.deferred_parameters.pop(VIDEO_TOOL, None)
        elif route.kind == "route":
            route.creative_brief.deferred_parameters[VIDEO_TOOL] = parameters
        source = next((ref.asset_id for ref in route.input_bindings if ref.slot == "source_image"), None)
        source = source or (choice.asset_id if choice else None) or self.goal.get("source_asset_id")
        if not source:
            images = self.context.get("vision_images", [])
            source = images[0]["asset_id"] if len(images) == 1 else None
        self.goal["source_asset_id"] = source
        # Discussion/clarification may return only a partial brief. It cannot replace the saved shot design.
        if not continuation or route.kind == "route":
            self.goal["creative_brief"] = route.creative_brief.model_dump()

    def ask(self, route, reason):
        if not self.goal or self.goal.get("output_kind") != "video":
            return reason
        self.goal.update(state="awaiting_choice", available_actions=[], warnings_zh=[reason] if reason else [])
        source = self.goal.get("source_asset_id")
        if source:
            self.goal["available_actions"].append("prepare_first_frame")
            role = self.request.image_roles.get(source, "auto")
            explicit = next((aid for aid, role in self.request.image_roles.items() if role == "first_frame"), None)
            if role in ("auto", "first_frame") and (not explicit or explicit == source):
                self.goal["available_actions"].insert(0, "direct_video")
        duration = self.goal.get("video_parameters", {}).get("duration_seconds")
        target = f"{duration} 秒视频" if duration else "视频"
        if not self.goal["available_actions"]:
            return f"最终目标仍是生成{target}。{reason}请提供可用图片或补充缺少的信息后再继续。"
        options = (
            "你希望直接尝试生成视频，还是先准备一张首帧？"
            if "direct_video" in self.goal["available_actions"]
            else "当前工具从整张图片开始，不能只提取角色身份。是否先准备符合目标的首帧？"
        )
        return f"最终目标仍是生成{target}。{reason}\n{options}"

    def metadata(self, route=None):
        if not self.goal:
            return {}
        if route and route.kind == "route" and self.goal.get("output_kind") == "video":
            self.goal["creative_brief"] = route.creative_brief.model_dump()
        return {"task_goal": deepcopy(self.goal), "workflow_actions": workflow_actions(self.goal)}

    def planned(self, route):
        if self.goal and self.goal.get("output_kind") == "video":
            step = "generate_video" if route.tool_id == VIDEO_TOOL else "prepare_first_frame"
            self.goal.update(
                current_step=step,
                state="awaiting_execution",
                available_actions=[],
                direct_video_approved=self.direct,
            )
        return self.metadata(route)
