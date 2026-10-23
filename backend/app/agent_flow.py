"""A bounded router/composer pipeline inside one persistent conversation turn."""

import json
from dataclasses import dataclass
from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy import select

from .agent_routing import ROUTING_SYSTEM, CreativeBrief, RouteInput, RouterResult
from .errors import AppError
from .models import AgentTurn, ContextSnapshot, LLMCall, Message, record_event
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
        self.trace = {"version": 1, "stages": []}

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
            ROUTING_SYSTEM, context, RouterResult.model_json_schema(), "tool_routing", 4096, repair
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
                value = model.model_validate_json(raw)
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
                if self.repaired:
                    raise AppError(
                        "plan_invalid", "本轮唯一修复后仍未通过校验，原草案已保留。请补充需求后重试。", 502
                    ) from exc
                self.repaired = True
                error = str(exc)[:2000]
                await self.stage(name)

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
        if route.kind != "route":
            if route.tool_id or route.input_bindings:
                raise ValueError("回复/澄清/能力不足时不能同时提出工具操作")
            return route
        if not route.tool_id or TOOL_MODES[route.tool_id] != route.task_mode:
            raise ValueError("工具和任务模式不匹配")
        if self.request.preferred_tool_id and route.tool_id != self.request.preferred_tool_id:
            raise ValueError("不能更换用户本轮限定的工具")
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
        if route.tool_id.startswith("video."):
            source = route.input_bindings[0].asset_id
            roles = self.request.image_roles
            explicit_first = next((aid for aid, role in roles.items() if role == "first_frame"), None)
            if roles.get(source, "auto") not in ("auto", "first_frame") or (
                explicit_first and source != explicit_first
            ):
                raise ValueError("主体/风格/构图参考不能当成视频首帧，明确首帧必须优先")
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
        self.validate_literals(route.creative_brief)
        # Parse the requested values now, so illegal settings can be repaired in routing.
        self.resolve_settings(route)
        return route

    def resolve_settings(self, route):
        source = self.source_parameters(route)
        requested = route.creative_brief.requested_parameters
        derived = (
            {"width", "height", "resolved_frames", "resolved_seconds"}
            if route.tool_id.startswith("video.")
            else {"resolution", "max_resolution"}
        )
        values = {k: v for k, v in source.items() if k not in derived | {"prompt", "instruction", "negative_prompt"}}
        allowed = set(PARAMETER_MODELS[route.tool_id].model_fields) - {"prompt", "instruction", "negative_prompt"}
        if not set(requested) <= allowed:
            raise ValueError("requested_parameters包含当前工具不支持或派生的参数")
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

    async def run(self):
        pending = self.context.get("pending_plan")
        if (
            self.request.revise_plan
            and self.request.revise_plan.rewrite_only
            and (pending.get("prompt_metadata") or {}).get("creative_brief")
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
            await self.validate_route(route, check_observations=False)
        else:
            route = await self.run_stage("routing", RouterResult, self.routing_request, self.validate_route)
            if route.kind != "route":
                await self.stage("validating")
                return GuidedOutcome(
                    "clarify" if route.kind == "clarify" else "explain", route.message_zh, self.visual_records(route)
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
                    "clarify" if composition.kind == "clarify" else "explain", composition.message_zh, observations
                )
            if composition.task_mode != route.task_mode:
                raise ValueError("不能改变已经固定的任务模式")
            brief = composition.creative_brief
            original = route.creative_brief
            for name in ("must_keep", "requested_changes", "must_not"):
                if not {x.text for x in getattr(original, name)} <= {x.text for x in getattr(brief, name)}:
                    raise ValueError("专用生成遗漏路由已提取的明确约束")
            if not {(x.text, x.kind) for x in original.literal_texts} <= {
                (x.text, x.kind) for x in brief.literal_texts
            }:
                raise ValueError("不能丢弃用户原文要求")
            self.validate_literals(brief)
            if brief.camera_policy != original.camera_policy or brief.audio_policy != original.audio_policy:
                raise ValueError("不能更改已确定的镜头或声音策略")
            if composition.constraint_review.conflicts:
                raise ValueError("仍有创作约束冲突时应澄清，不能提出可执行方案")
            params = composition.parameters.model_dump(exclude_unset=True)
            defaults = {k: v for k, v in resolved.items() if k in composition.parameters.model_fields}
            params = {**defaults, **params}
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
            # Display stable creative changes instead of structural repair commentary.
            if brief.must_keep or brief.requested_changes:
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
