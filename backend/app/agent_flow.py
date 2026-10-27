"""A bounded router/composer pipeline inside one persistent conversation turn."""

import json
from dataclasses import dataclass
from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy import select

from .agent_routing import ROUTING_SYSTEM, CreativeBrief, RouteInput, RouterResult
from .creative_policy import CreativePolicy
from .errors import AppError
from .goal_tracking import VideoGoalTracker
from .models import AgentTurn, ContextSnapshot, LLMCall, Message, Plan, record_event
from .prompt_compile import compile_document, generation_metadata
from .prompt_composition import COMPOSITION_SYSTEM, composition_model
from .prompt_guides import TOOL_MODES
from .schemas import PARAMETER_MODELS


@dataclass
class GuidedOutcome:
    kind: str
    message: str
    observations: list
    operation: dict | None = None
    metadata: dict | None = None
    based_on_plan_id: str | None = None
    based_on_execution_id: str | None = None
    change_summary: str = ""


class GuidedFlow:
    def __init__(self, planner, turn_id, key, context, request, images, user_message_id):
        self.p, self.turn_id, self.key = planner, turn_id, key
        self.context, self.request, self.images = context, request, images
        self.user_message_id = user_message_id
        self.repaired = False
        self.policy = CreativePolicy(context, request, planner.db)
        self.goals = VideoGoalTracker(context, request)
        self.trace = {"version": 2, "stages": [], "constraint_provenance": self.policy.audit}

    @staticmethod
    def validation_reason(exc):
        if isinstance(exc, ValidationError):
            issues = exc.errors(include_url=False, include_input=False, include_context=False)
            return "；".join(
                f"{'.'.join(map(str, issue['loc'])) or '输出内容'}：{issue['msg']}" for issue in issues[:4]
            )
        return str(exc)[:1200]

    async def invalid_plan(self, name, reason):
        pending = self.context.get("pending_plan")
        preserved = False
        if pending:
            async with self.p.db.session() as session:
                preserved = bool(
                    await session.scalar(
                        select(Plan.id).where(Plan.id == pending["id"], Plan.state == "draft").limit(1)
                    )
                )
        stage_label = "工具选择" if name == "routing" else "提示词编写"
        outcome = "已有待确认方案仍保留。" if preserved else "本轮未创建可执行方案。"
        message = f"{stage_label}未通过校验：{reason}。本轮自动修复额度已用完，未提交生成任务。{outcome}可以重试本轮或使用手动工具。"
        return AppError(
            "plan_invalid",
            message,
            502,
            details={"stage": name, "validation_errors": self.trace["validation_errors"], "draft_preserved": preserved},
        )

    def parse_stage_result(self, model, raw, name):
        changes = []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Only a complete final value with one unclosed root object may be closed.
            # Never finish strings, invent values, trim text, or accept a partial nested object.
            stack, quoted, escaped, matched = [], False, False, True
            for char in raw:
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "{[":
                    stack.append(char)
                elif char in "}]":
                    if not stack or stack.pop() != ("{" if char == "}" else "["):
                        matched = False
                        break
            if matched and not quoted and stack == ["{"] and raw.rstrip().endswith("}"):
                try:
                    payload = json.loads(raw + "}")
                    changes.append("close_root_object")
                except json.JSONDecodeError:
                    return model.model_validate_json(raw)
            else:
                return model.model_validate_json(raw)
        if isinstance(payload, dict) and isinstance(payload.get("creative_brief"), dict):
            brief = dict(payload["creative_brief"])
            for key in ("based_on_plan_id", "based_on_execution_id", "frame_assessment", "shot_review"):
                if key not in brief or key not in model.model_fields:
                    continue
                if key in payload and payload[key] != brief[key]:
                    raise ValueError(f"模型输出的{key}在不同层级互相冲突")
                payload[key] = brief.pop(key)
                changes.append(f"move_creative_brief.{key}")
            payload["creative_brief"] = brief
        if name == "composing" and isinstance(payload, dict):
            document = payload.get("prompt_document")
            if isinstance(document, dict) and isinstance(document.get("shots"), list):
                for index, shot in enumerate(document["shots"]):
                    # A known empty annotation is not creative content or an execution parameter.
                    # Keep rejecting unknown fields and nonempty annotations.
                    if isinstance(shot, dict) and "cut_at_seconds_note_removed" in shot:
                        if shot["cut_at_seconds_note_removed"] is None:
                            shot.pop("cut_at_seconds_note_removed")
                            changes.append(f"drop_empty_shot_annotation.{index}")
        value = model.model_validate(payload)
        if changes:
            self.trace.setdefault("format_normalizations", []).append({"stage": name, "changes": changes})
        return value

    def model_request(self, instructions, context, schema, name, output_limit, repair=None):
        content = [{"type": "input_text", "text": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]
        for image in self.images:
            content.extend(image.content_parts())
        return {
            "model": self.p.settings.llm_model,
            "instructions": instructions + ("\n仅修复当前阶段错误，不改变用户目标：" + repair if repair else ""),
            "input": [{"role": "user", "content": content}],
            "reasoning": {"effort": self.p.settings.reasoning_effort},
            "text": {"format": {"type": "json_schema", "name": name, "schema": schema}},
            "max_output_tokens": min(output_limit, self.p.settings.max_output_tokens),
        }

    def routing_request(self, context, repair=None):
        context = {key: value for key, value in context.items() if not key.startswith("_")}
        return self.model_request(
            ROUTING_SYSTEM, context, RouterResult.model_json_schema(), "tool_routing", 6144, repair
        )

    async def stage(self, name):
        async with self.p.db.transaction() as session:
            turn = await session.get(AgentTurn, self.turn_id)
            turn.stage = name
            snapshot = await session.get(ContextSnapshot, turn.context_id)
            snapshot.data = {**self.context, "_agent": self.trace}
            record_event(session, "turn.updated", turn.project_id, turn.id, stage=name)

    async def compact(self, builder):
        # Read the current summary boundary afresh; a previous phase may have compacted it.
        async with self.p.db.session() as session:
            from .conversations import memory_key
            from .models import Setting

            memory_row = await session.get(Setting, memory_key(self.context["conversation"]["id"]))
            memory = memory_row.value if memory_row else {}
            messages = (
                await session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == self.context["conversation"]["id"],
                        Message.id != self.user_message_id,
                    )
                    .order_by(Message.created_at, Message.id)
                )
            ).all()
            if memory:
                messages = [
                    m
                    for m in messages
                    if (m.created_at, m.id) > (memory["through_created_at"], memory["through_message_id"])
                ]
        await self.p.compact_context(
            self.turn_id,
            self.key,
            self.context["project"]["id"],
            self.context,
            messages,
            memory,
            {e["id"] for e in self.context["executions"] if e.get("pinned")},
            self.images,
            request_builder=builder,
        )

    async def run_stage(self, name, model, builder, validate):
        await self.stage(name)
        await self.compact(lambda context: builder(context, None))
        error = None
        while True:
            try:
                raw = await self.p.invoke(
                    self.turn_id,
                    self.key,
                    builder(self.context, error),
                    purpose="repair" if error else ("composition" if name == "composing" else "routing"),
                )
                value = self.parse_stage_result(model, raw, name)
                if name == "composing":
                    await self.stage("validating")
                result = await validate(value)
                self.trace["stages"].append(
                    {"stage": name, "result": value.model_dump(), "repaired": error is not None}
                )
                return result
            except (ValidationError, ValueError, AppError) as exc:
                if isinstance(exc, AppError) and exc.code not in (
                    "invalid_parameters",
                    "resource_limit",
                    "dimensions",
                    "input_slots",
                    "seed_range",
                ):
                    raise
                reason = self.validation_reason(exc)
                self.trace.setdefault("validation_errors", []).append({"stage": name, "reason": reason})
                await self.stage(name)
                if self.repaired:
                    failure = await self.invalid_plan(name, reason)
                    raise failure from exc
                self.repaired = True
                error = reason[:2000]

    def visual_records(self, value):
        return self.p.visual_records(
            SimpleNamespace(visual_observations=value.visual_observations, operation=None), self.images
        )

    def source_parameters(self, route):
        pending = self.context.get("pending_plan")
        if route.based_on_plan_id:
            if not pending or pending["id"] != route.based_on_plan_id:
                raise ValueError("修改的方案不是当前对话草案")
            if pending["tool_id"] != route.tool_id:
                raise ValueError("跨工具需要创建新方案，不能修改原方案身份")
            return dict(pending["parameters"])
        if route.based_on_execution_id:
            source = next((e for e in self.context["executions"] if e["id"] == route.based_on_execution_id), None)
            if not source:
                raise ValueError("来源执行不在当前对话或明确引用的上下文中")
            return dict(source["parameters"]) if source["tool_id"] == route.tool_id else {}
        return {}

    def validate_literals(self, brief):
        sources = [self.context["user_request"], self.context["project"]["goal"], self.context["project"]["style"]]
        sources += [item["content"] for item in self.context["recent_messages"]]
        if self.context.get("history_summary"):
            sources.append(self.context["history_summary"]["summary"])
        pending = self.context.get("pending_plan")
        if pending:
            sources += [str(pending["parameters"].get(key, "")) for key in ("prompt", "instruction")]
        for literal in brief.literal_texts:
            if not any(literal.text in source for source in sources):
                raise ValueError("台词或画面文字在用户要求/当前对话中没有原文依据")

    async def validate_route(self, route, check_observations=True):
        if check_observations:
            self.visual_records(route)
        await self.goals.bind(route, self.policy)
        if route.kind != "route":
            if route.tool_id or route.input_bindings:
                raise ValueError("回复/澄清/能力不足时不能同时提出工具操作")
            if route.kind == "clarify" and self.goals.goal:
                if route.clarification_kind == "frame_choice":
                    route.message_zh = self.goals.ask(route, route.message_zh)
                else:
                    self.goals.goal.update(state="awaiting_clarification", available_actions=[])
            return route
        if not route.tool_id or TOOL_MODES[route.tool_id] != route.task_mode:
            raise ValueError("工具和任务模式不匹配")
        tool = self.p.registry.get(route.tool_id)
        self.p.guides.get(route.tool_id, tool["version"], route.task_mode)
        if tool["availability"] != "available":
            raise ValueError("工具不可用，应向用户说明而不是提出方案")
        expected = {item["slot"] for item in tool["inputs"]}
        if len(route.input_bindings) != len(expected) or {item.slot for item in route.input_bindings} != expected:
            raise ValueError("输入槽位不完整或重复")
        assets = {item["id"]: item for item in self.context["assets"]}
        known_executions = {item["id"] for item in self.context["executions"] if item["state"] == "succeeded"}
        for ref in route.input_bindings:
            asset = assets.get(ref.asset_id)
            if not asset or asset["media_type"] != "image" or asset["presence"] != "available":
                raise ValueError("输入必须是当前上下文里可读取的真实图片")
            if not asset["selected"] and not self.request.revise_plan:
                if not ref.reference_text or ref.reference_text not in self.context["user_request"]:
                    raise ValueError("使用未选中的图片时须逐字引用用户本轮指代")
                if asset["execution_id"] not in known_executions and asset["name"] not in self.context["user_request"]:
                    raise ValueError("不能任意使用未明确点名的素材库图片")
        if (
            self.goals.goal
            and self.goals.goal.get("output_kind") == "video"
            and route.tool_id.startswith("image.")
            and not self.goals.preparation_approved
        ):
            message = self.goals.ask(route, "建议先编辑首帧以改善开场和动作衔接；这一步会先生成图片，视频尚未生成。")
            return RouterResult(kind="clarify", message_zh=message, visual_observations=route.visual_observations)
        if self.request.preferred_tool_id and route.tool_id != self.request.preferred_tool_id:
            raise ValueError("不能更换用户本轮限定的工具")
        if route.tool_id.startswith("video."):
            source = route.input_bindings[0].asset_id
            roles = self.request.image_roles
            explicit_first = next((aid for aid, role in roles.items() if role == "first_frame"), None)
            if roles.get(source, "auto") not in ("auto", "first_frame") or (
                explicit_first and source != explicit_first
            ):
                message = self.goals.ask(route, "这张图片被明确指定为参考，不能直接充当整张视频首帧。")
                return RouterResult(kind="clarify", message_zh=message, visual_observations=route.visual_observations)
        if self.request.revise_plan:
            if route.based_on_plan_id != self.request.revise_plan.plan_id:
                raise ValueError("明确调整此方案时必须关联该方案，不能另起方案身份")
            if self.request.revise_plan.rewrite_only:
                pending = self.context["pending_plan"]
                if [{"slot": r.slot, "asset_id": r.asset_id} for r in route.input_bindings] != pending["inputs"]:
                    raise ValueError("仅改写提示词时不能改变输入图片")
                if any(pending["parameters"].get(k) != v for k, v in route.creative_brief.requested_parameters.items()):
                    raise ValueError("仅改写提示词时不能改变原方案执行参数")
        self.source_parameters(route)
        await self.policy.ground_constraints(route.creative_brief, route.visual_observations)
        self.validate_literals(route.creative_brief)
        # Parse the requested values now, so illegal settings can be repaired in routing.
        self.resolve_settings(route)
        issue = await self.policy.validate_intent(route)
        shot = route.creative_brief.shot_intent
        if issue and not (self.goals.direct and shot and shot.scene.policy == "plain" and shot.mode != "uncertain"):
            if shot and shot.mode != "uncertain":
                issue = self.goals.ask(route, issue)
            return RouterResult(kind="clarify", message_zh=issue, visual_observations=route.visual_observations)
        if not issue and route.tool_id.startswith("video.") and route.frame_assessment is not None:
            if route.input_bindings[0].asset_id in {image.metadata["asset_id"] for image in self.images}:
                issue = self.policy.frame_issue(route, route.frame_assessment)
        if issue and not (route.tool_id.startswith("video.") and self.goals.direct):
            issue = self.goals.ask(route, issue)
            return RouterResult(kind="clarify", message_zh=issue, visual_observations=route.visual_observations)
        if issue and self.goals.goal:
            self.goals.goal["warnings_zh"] = [issue]
        return route

    def resolve_settings(self, route):
        source = self.source_parameters(route)
        requested = route.creative_brief.requested_parameters
        # Duration belongs to the later video, never to a preparatory image operation.
        if route.tool_id.startswith(("image.generate", "image.edit")) and "duration_seconds" in requested:
            duration = requested["duration_seconds"]
            video_id = "video.generate.minimax_i2v"
            deferred = route.creative_brief.deferred_parameters.setdefault(video_id, {})
            if "duration_seconds" in deferred and deferred["duration_seconds"] != duration:
                raise ValueError("当前记录的后续视频时长互相冲突，请明确目标时长")
            deferred["duration_seconds"] = duration
            del requested["duration_seconds"]
        self.validate_deferred_parameters(route)
        derived = (
            {"width", "height", "resolved_frames", "resolved_seconds"}
            if route.tool_id.startswith("video.")
            else {"resolution", "max_resolution"}
        )
        values = {k: v for k, v in source.items() if k not in derived | {"prompt", "instruction", "negative_prompt"}}
        allowed = set(PARAMETER_MODELS[route.tool_id].model_fields) - {"prompt", "instruction", "negative_prompt"}
        unsupported = set(requested) - allowed
        if unsupported:
            raise ValueError(f"当前工具不支持这些执行参数：{'、'.join(sorted(unsupported))}")
        values.update(requested)
        required_text = (
            {"prompt": "_"}
            if route.tool_id.startswith(("image.generate", "video."))
            else {"instruction": "_"}
            if route.tool_id.startswith("image.edit")
            else {}
        )
        parsed = PARAMETER_MODELS[route.tool_id].model_validate({**required_text, **values}).model_dump()
        if route.tool_id.startswith("video."):
            asset = next(a for a in self.context["assets"] if a["id"] == route.input_bindings[0].asset_id)
            parsed.update(self.p.registry.resolve_video(parsed, asset["media_info"], self.p.settings))
        return parsed

    def validate_deferred_parameters(self, route):
        for tool_id, parameters in route.creative_brief.deferred_parameters.items():
            if tool_id == route.tool_id:
                raise ValueError("后续参数不能与当前工具重复，请放入当前执行参数")
            model = PARAMETER_MODELS[tool_id]
            allowed = set(model.model_fields) - {"prompt", "instruction", "negative_prompt"}
            if unsupported := set(parameters) - allowed:
                raise ValueError(f"后续工具不支持这些参数：{'、'.join(sorted(unsupported))}")
            text = {key: "_" for key in ("prompt", "instruction") if key in model.model_fields}
            model.model_validate({**text, **parameters})

    def inherit_brief(self, original, supplied):
        brief = original.model_copy(deep=True)
        if supplied is None:
            return brief
        if supplied.camera_policy not in ("unspecified", original.camera_policy) and not (
            original.camera_policy == "unspecified" and supplied.camera_policy == "static"
        ):
            raise ValueError("提示词编写改变了已确定的镜头策略")
        for name in supplied.audio_policy.model_fields_set:
            value = getattr(supplied.audio_policy, name)
            expected = getattr(original.audio_policy, name)
            if value is not None and value != expected and not (expected is None and value is False):
                raise ValueError("提示词编写改变了已确定的声音策略")
        for name in ("must_keep", "requested_changes", "must_not", "literal_texts"):
            items = getattr(brief, name)
            keys = {(item.text, getattr(item, "kind", None)) for item in items}
            for item in getattr(supplied, name):
                key = (item.text, getattr(item, "kind", None))
                if key not in keys:
                    items.append(item.model_copy(deep=True))
                    keys.add(key)
        brief.assumptions_zh = list(dict.fromkeys(brief.assumptions_zh + supplied.assumptions_zh))
        return CreativeBrief.model_validate(brief.model_dump())

    async def run(self):
        pending = self.context.get("pending_plan")
        if (
            self.request.revise_plan
            and self.request.revise_plan.rewrite_only
            and (pending.get("prompt_metadata") or {}).get("creative_brief")
            and (
                not (
                    pending["tool_id"].startswith("video.")
                    or "video.generate.minimax_i2v"
                    in (pending["prompt_metadata"]["creative_brief"].get("deferred_parameters") or {})
                )
                or pending["prompt_metadata"]["creative_brief"].get("shot_intent")
            )
        ):
            brief = CreativeBrief.model_validate((pending.get("prompt_metadata") or {}).get("creative_brief") or {})
            brief.requested_parameters = {
                k: v
                for k, v in pending["parameters"].items()
                if k
                not in {
                    "prompt",
                    "instruction",
                    "negative_prompt",
                    "width",
                    "height",
                    "resolved_frames",
                    "resolved_seconds",
                    "resolution",
                    "max_resolution",
                }
            }
            # Image generation dimensions are executable parameters, not derived video fields.
            if pending["tool_id"].startswith("image.generate"):
                brief.requested_parameters.update({k: pending["parameters"][k] for k in ("width", "height")})
            route = RouterResult(
                kind="route",
                message_zh="按当前方案重新编写提示词，保持输入和执行参数。",
                tool_id=pending["tool_id"],
                task_mode=TOOL_MODES[pending["tool_id"]],
                selection_reason_zh="沿用明确指定的当前方案工具",
                creative_brief=brief,
                input_bindings=[RouteInput(**ref) for ref in pending["inputs"]],
                based_on_plan_id=pending["id"],
            )
            # Observations are generated by the composer; this deterministic path has no model observations.
            for ref in route.input_bindings:
                next(a for a in self.context["assets"] if a["id"] == ref.asset_id)["selected"] = True
            route = await self.validate_route(route, check_observations=False)
        else:
            route = await self.run_stage("routing", RouterResult, self.routing_request, self.validate_route)
        if route.kind != "route":
            await self.stage("validating")
            return GuidedOutcome(
                "clarify" if route.kind == "clarify" else "explain",
                route.message_zh,
                self.visual_records(route) if route.visual_observations else [],
                metadata=self.goals.metadata(route),
            )
        tool = self.p.registry.get(route.tool_id)
        guide = self.p.guides.get(route.tool_id, tool["version"], route.task_mode)
        source_parameters = self.source_parameters(route)
        resolved = self.resolve_settings(route)
        bound_ids = [ref.asset_id for ref in route.input_bindings]
        all_ids = list(dict.fromkeys([image.metadata["asset_id"] for image in self.images] + bound_ids))
        if len(all_ids) > self.p.settings.vision_max_images:
            raise AppError("vision_limit", "实际执行输入与已附图合计超过读图上限，请减少选择。")
        assets = {a["id"]: a for a in self.context["assets"]}
        for aid in bound_ids:
            assets[aid]["selected"] = True
            if aid not in {image.metadata["asset_id"] for image in self.images}:
                assets[aid]["reference_source"] = "resolved_input"
        self.images = await self.p.prepare_vision(self.context, all_ids, self.request.image_roles)
        self.context["vision_images"] = [image.metadata for image in self.images]
        self.context["_agent"] = self.trace
        self.trace["route"] = route.model_dump()
        bindings = [
            {
                "slot": ref.slot,
                "asset_id": ref.asset_id,
                "blob_id": next(
                    image.metadata["blob_id"] for image in self.images if image.metadata["asset_id"] == ref.asset_id
                ),
                "purpose": self.request.image_roles.get(ref.asset_id, "auto"),
                "label": "<Picture 1>" if route.task_mode == "video_i2va" else None,
            }
            for ref in route.input_bindings
        ]
        async with self.p.db.transaction() as session:
            message = await session.get(Message, self.user_message_id)
            message.details = {
                **message.details,
                "vision_images": self.context["vision_images"],
                "composition_input_ids": bound_ids,
            }
        model = composition_model(route.tool_id)
        selected_card = next(card for card in self.context["tools"] if card["id"] == route.tool_id)

        def build(context, repair=None):
            selected_context = {k: v for k, v in context.items() if k != "tools" and not k.startswith("_")}
            selected_context.update(
                selected_tool=selected_card,
                route=route.model_dump(),
                resolved_execution_settings=resolved,
                asset_binding_map=bindings,
                guide_examples=guide["examples"],
                video_goal=self.goals.goal,
                direct_video_approved=self.goals.direct,
            )
            return self.model_request(
                COMPOSITION_SYSTEM + "\n\n" + guide["text"],
                selected_context,
                model.model_json_schema(),
                "guided_composition",
                8192,
                repair,
            )

        async def validate(composition):
            observations = self.visual_records(composition)
            if composition.kind != "propose":
                return GuidedOutcome(
                    "clarify" if composition.kind == "clarify" else "explain",
                    composition.message_zh,
                    observations,
                    metadata=self.goals.metadata(route),
                )
            if composition.task_mode != route.task_mode:
                raise ValueError("不能改变已经固定的任务模式")
            brief = self.inherit_brief(route.creative_brief, composition.creative_brief)
            composition.creative_brief = brief
            await self.policy.ground_constraints(brief, composition.visual_observations)
            self.validate_literals(brief)
            if route.tool_id.startswith("video."):
                issue = self.policy.frame_issue(route, composition.frame_assessment)
                if issue and not self.goals.direct:
                    return GuidedOutcome(
                        "clarify", self.goals.ask(route, issue), observations, metadata=self.goals.metadata(route)
                    )
                if issue and self.goals.goal:
                    self.goals.goal["warnings_zh"] = list(
                        dict.fromkeys(self.goals.goal.get("warnings_zh", []) + [issue])
                    )
            if composition.constraint_review.conflicts:
                return GuidedOutcome(
                    "clarify",
                    "当前方案仍有需要你取舍的要求：\n"
                    + "\n".join(f"• {item}" for item in composition.constraint_review.conflicts)
                    + "\n请明确如何取舍后再准备方案，本轮没有提交生成任务。",
                    observations,
                )
            self.policy.validate_shot_review(route, composition.shot_review)
            params = composition.parameters.model_dump(exclude_unset=True)
            defaults = {k: v for k, v in resolved.items() if k in composition.parameters.model_fields}
            params = {**defaults, **params}
            if any(
                params.get(key) != resolved[key] for key in route.creative_brief.requested_parameters if key != "seed"
            ):
                raise ValueError("提示词编写改变了用户明确指定的执行参数")
            if route.task_mode == "video_i2va":
                if any(params[k] != resolved[k] for k in ("duration_seconds", "megapixels", "aspect_ratio")):
                    raise ValueError("视频时长、面积和比例已在编写前冻结，不能改变")
            if self.request.revise_plan and self.request.revise_plan.rewrite_only:
                if any(params.get(k) != v for k, v in defaults.items() if k != "seed"):
                    raise ValueError("仅改写提示词时不能改变执行参数")
                if composition.candidate_count != pending["candidate_count"] or composition.seed_policy == "new":
                    raise ValueError("仅改写提示词时不能改变候选数量或随机种子")
            elif "seed" in route.creative_brief.requested_parameters:
                requested_seed = route.creative_brief.requested_parameters["seed"]
                if requested_seed is None and composition.seed_policy != "new":
                    raise ValueError("用户要求新随机种子时应采用new策略")
                if requested_seed is not None and (
                    composition.seed_policy != "explicit" or params.get("seed") != requested_seed
                ):
                    raise ValueError("用户明确指定的种子必须原样使用，并采用explicit策略")
            values, sections = compile_document(route.tool_id, composition.prompt_document, brief, resolved, bindings)
            params.update(values)
            if composition.seed_policy == "new":
                params["seed"] = None
            elif composition.seed_policy == "inherit" and source_parameters:
                params["seed"] = source_parameters["seed"]
            elif composition.seed_policy == "explicit" and params.get("seed") is None:
                raise ValueError("explicit策略需要明确种子")
            self.p.registry.normalize(
                route.tool_id,
                params,
                {ref.slot: assets[ref.asset_id] for ref in route.input_bindings},
                self.p.settings,
                composition.candidate_count,
            )
            metadata = generation_metadata(guide, route, composition, bindings, params, sections)
            metadata.update(self.goals.planned(route))
            metadata["constraint_provenance"] = list(self.policy.audit)
            metadata["assumptions_zh"] = list(dict.fromkeys(metadata["assumptions_zh"] + self.policy.suggestions))
            # Display stable creative changes instead of structural repair commentary.
            metadata["constraint_review"] = {
                "kept": [item.text for item in brief.must_keep],
                "changed": [item.text for item in brief.requested_changes],
                "conflicts": [],
            }
            return GuidedOutcome(
                "propose",
                composition.message_zh,
                observations,
                {
                    "tool_id": route.tool_id,
                    "inputs": [{"slot": r.slot, "asset_id": r.asset_id} for r in route.input_bindings],
                    "parameters": params,
                    "candidate_count": composition.candidate_count,
                },
                metadata,
                route.based_on_plan_id,
                route.based_on_execution_id,
                "；".join(item.text for item in brief.requested_changes) or composition.change_summary_zh,
            )

        outcome = await self.run_stage("composing", model, build, validate)
        if outcome.metadata:
            async with self.p.db.session() as session:
                calls = (
                    await session.scalars(
                        select(LLMCall).where(LLMCall.turn_id == self.turn_id).order_by(LLMCall.created_at)
                    )
                ).all()
            outcome.metadata["call_ids"] = [
                {"id": call.id, "purpose": call.request_data.get("_aiv", {}).get("purpose")} for call in calls
            ]
        await self.stage("validating")
        return outcome
