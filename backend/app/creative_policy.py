"""Ground creative constraints and review declared shot suitability, not rendered quality."""

from sqlalchemy import func, select

from .models import Message

VIDEO_TOOL = "video.generate.minimax_i2v"


class CreativePolicy:
    def __init__(self, context, request, db):
        self.context, self.request, self.db = context, request, db
        self.audit = []
        self.suggestions = []
        self.evidence_cache = {}

    async def evidence_valid(self, quote, source="user"):
        if not quote or not quote.strip():
            return False
        if source == "project":
            return any(quote in self.context["project"].get(name, "") for name in ("goal", "style"))
        if source not in ("user", "history"):
            return False
        if quote in self.context["user_request"]:
            return True
        # Assistant claims and model summaries cannot prove what a user required.
        if quote not in self.evidence_cache:
            async with self.db.session() as session:
                self.evidence_cache[quote] = bool(
                    await session.scalar(
                        select(Message.id)
                        .where(
                            Message.conversation_id == self.context["conversation"]["id"],
                            Message.role == "user",
                            func.instr(Message.content, quote) > 0,
                        )
                        .limit(1)
                    )
                )
        return self.evidence_cache[quote]

    async def ground_constraints(self, brief, observations=()):
        observed = {item.asset_id: item for item in observations}
        for name in ("must_keep", "requested_changes", "must_not"):
            grounded = []
            for item in getattr(brief, name):
                quote = item.evidence or item.text  # Exact legacy user text remains usable.
                if await self.evidence_valid(quote, item.source):
                    grounded.append(item)
                    continue
                visual = observed.get(item.asset_id)
                soft = item.source == "assumption" or (
                    item.source == "observation" and visual is not None and quote in visual.observation
                )
                if soft and name != "must_not" and item.text not in self.suggestions:
                    # Observation/creative choices remain visible, never user-authored prohibitions.
                    self.suggestions.append(item.text)
                entry = {
                    "field": name,
                    "text": item.text,
                    "source": item.source,
                    "evidence": item.evidence,
                    "disposition": "suggestion" if soft else "unverified",
                }
                if entry not in self.audit:
                    self.audit.append(entry)
            setattr(brief, name, grounded)

    @staticmethod
    def needs_shot_design(route):
        return route.tool_id == VIDEO_TOOL or VIDEO_TOOL in route.creative_brief.deferred_parameters

    async def validate_intent(self, route):
        if not self.needs_shot_design(route):
            return None
        shot = route.creative_brief.shot_intent
        if shot is None:
            raise ValueError("视频及首帧准备需要明确动作起点、主要变化、结束状态和场景设计")
        if not await self.evidence_valid(shot.intent_quote):
            raise ValueError("镜头目标必须引用当前对话的真实用户需求，不能只引用助手旧回复")
        if shot.mode == "uncertain":
            return "需要先明确这个镜头：你希望看到动作发生的过程，还是动作完成后保持姿态？确认后我再准备相应起始画面。"
        if shot.mode == "hold" and not await self.evidence_valid(shot.hold_evidence):
            raise ValueError("保持姿态/微动模式需要真实用户原话依据，不能自行取消主要动作")
        if not shot.start_state_zh.strip() or not shot.end_state_zh.strip():
            raise ValueError("镜头必须说明起始状态和结束状态")
        if shot.mode == "action" and (
            not shot.progression_zh.strip() or shot.start_state_zh.strip() == shot.end_state_zh.strip()
        ):
            raise ValueError("动作镜头必须有主要变化，不能把同一完成姿态当作动作前后状态")
        scene = shot.scene
        if scene.policy == "unspecified" or not scene.description_zh.strip():
            raise ValueError("镜头需要明确场景和光照；未指定时应给出可确认的具体场景建议")
        if scene.requires_surface and not scene.surface_zh.strip():
            raise ValueError("涉及接触的动作需要明确实际地面或接触面")
        if scene.explicit_background and not await self.evidence_valid(scene.evidence, scene.source):
            raise ValueError("明确背景要求没有用户或项目原文依据")
        if scene.source == "observation":
            observed = {item.asset_id for item in route.visual_observations}
            scene.asset_id = scene.asset_id or (route.frame_assessment.asset_id if route.frame_assessment else None)
            if scene.asset_id not in observed:
                raise ValueError("场景观察必须关联本阶段实际看到的输入图片")
        if scene.policy == "plain" and not scene.explicit_background:
            return "输入使用纯色背景，场景和动作衔接可能受到限制。可保留原图直接尝试，也可先准备带环境的首帧。"
        return None

    def frame_issue(self, route, assessment):
        shot = route.creative_brief.shot_intent
        if assessment is None:
            raise ValueError("视频方案需要基于实际输入图片评估首帧，不能只判断提示词格式")
        source_id = route.input_bindings[0].asset_id
        if assessment.asset_id != source_id:
            raise ValueError("首帧评估必须对应实际执行输入")
        reasons = []
        if assessment.background_type == "sheet":
            reasons.append("输入仍为多视图排版")
        if assessment.background_type == "unknown":
            reasons.append("无法确认当前图片的场景是否可用")
        if shot.mode == "action" and assessment.action_phase in ("after", "hold"):
            reasons.append("当前画面已经是目标动作完成后的姿态")
        if shot.mode == "action" and assessment.action_phase == "unknown":
            reasons.append("无法确认当前画面相对目标动作所处的阶段")
        if assessment.action_compatible is not True:
            reasons.append("无法确认当前姿态能衔接目标动作")
        if assessment.scene_matches is not True:
            reasons.append("当前背景与镜头场景不匹配或缺少环境")
        if shot.scene.requires_surface and assessment.surface_visible is not True:
            reasons.append("看不到动作所需的地面或接触面")
        explicit_frame = self.request.image_roles.get(source_id) == "first_frame"
        if assessment.background_type == "plain" and not (shot.scene.explicit_background or explicit_frame):
            reasons.append("角色图的纯色底尚未明确作为视频场景")
        if not reasons:
            return None
        detail = "；".join(reasons)
        return f"根据当前图片判断，直接生成可能影响效果：{detail}。可先编辑首帧改善衔接；这是效果建议，不代表视频工具无法接收这张图片。"

    def validate_shot_review(self, route, review):
        if not self.needs_shot_design(route):
            return
        if review is None:
            raise ValueError("需要检查实际提示词是否落实主要动作、状态衔接和场景")
        if route.tool_id == VIDEO_TOOL:
            covered = review.main_action_present
        else:
            # A still starting frame must not already perform the entire future action.
            covered = review.start_frame_prepared is not False
        if not (covered and review.start_to_end_consistent and review.scene_addressed):
            raise ValueError("提示词的主要动作、首尾状态或场景仍有未落实项，应澄清而不是提出执行方案")
