"""Typed documents for the selected tool, separate from executable parameters."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, create_model

from .agent_routing import BASE_SYSTEM, CreativeBrief, TaskMode, VisualObservation
from .schemas import PARAMETER_MODELS, StrictModel

COMPOSITION_SYSTEM = (
    BASE_SYSTEM
    + """
本阶段为已经固定的工具生成一次方案，只使用selected_tool及专用guide，不自行切换工具。原始用户需求优先于摘要；必要信息缺失时kind=clarify；只讨论时kind=reply。
kind=propose时填写prompt_document、parameters、中文创作摘要和检查项。参数不能包含由后台计算的width/height/resolved_frames等派生视频值；resolved_execution_settings给出冻结值。图像生成宽高按自身Schema提供，视频派生值由后台核算。
creative_brief必须完整保留路由已提取的明确约束和literal_texts，补足快路径原文中的要求；不得将它们弱化为建议。保守补充并明确assumptions_zh。
prompt_document按当前工具Schema书写，模型正文默认英文，原文台词/歌词/画面文字保持原语言、措辞和标点。说明、约束摘要和review为中文。
同工具小改保留未改变的来源参数和种子。seed_policy=inherit继承明确来源、new探索新结果、explicit使用用户明确种子；跨工具不声称种子等价。
每张本阶段附图都返回一项真实visual_observations。只使用asset_binding_map内的生成模型引用标签；辅助图像不拥有额外的Picture编号。
constraint_review只写简短的保持项、已落实修改、必要补充及冲突，不写思维链。不把模板示例的剧情、对白、人物和音乐抄进当前作品。
所有对象只输出Schema中已有字段；镜头对象只能有index、cut_at_seconds、description，不能新增note、description_note等字段，空字段也不例外。
message_zh、change_summary_zh、creative_summary_zh、constraint_review只描述作品和创作决定。修复Schema字段或格式是内部步骤，不向用户叙述“删除字段、修复JSON”等实现细节。
"""
)


class ImageDocument(StrictModel):
    positive_prompt: str = Field(min_length=1, max_length=12000)
    negative_prompt: str = Field(default="", max_length=8000)


class EditDocument(StrictModel):
    instruction: str = Field(min_length=1, max_length=12000)
    keep: list[str] = Field(default_factory=list, max_length=30)
    changes: list[str] = Field(default_factory=list, max_length=30)
    negative_prompt: str = Field(default="", max_length=8000)


class Shot(StrictModel):
    index: int = Field(ge=1, le=20)
    cut_at_seconds: float | None = Field(default=None, ge=0)
    description: str = Field(min_length=1, max_length=11000)


class VideoDocument(StrictModel):
    shots: list[Shot] = Field(min_length=1, max_length=20)
    overall_soundscape: str = Field(min_length=1, max_length=4000)
    non_diegetic_music: str = Field(min_length=1, max_length=3000)


class ConstraintReview(StrictModel):
    kept: list[str] = Field(default_factory=list, max_length=20)
    changed: list[str] = Field(default_factory=list, max_length=20)
    conflicts: list[str] = Field(default_factory=list, max_length=12)


class CompositionBase(StrictModel):
    kind: Literal["propose", "clarify", "reply"]
    message_zh: str = Field(min_length=1, max_length=4000)
    change_summary_zh: str = Field(default="", max_length=4000)
    selection_reason_zh: str = Field(default="", max_length=1000)
    assumptions_zh: list[str] = Field(default_factory=list, max_length=12)
    creative_summary_zh: str = Field(default="", max_length=2000)
    creative_brief: CreativeBrief = Field(default_factory=CreativeBrief)
    constraint_review: ConstraintReview = Field(default_factory=ConstraintReview)
    visual_observations: list[VisualObservation] = Field(default_factory=list, max_length=12)
    seed_policy: Literal["inherit", "new", "explicit"] = "inherit"
    candidate_count: int = Field(default=1, ge=1, le=4)


@lru_cache(maxsize=6)
def composition_model(tool_id):
    parameters = PARAMETER_MODELS[tool_id]
    # Text belongs in the document, not in a second conflicting parameter value.
    fields = {
        name: (field.annotation, field)
        for name, field in parameters.model_fields.items()
        if name not in ("prompt", "instruction", "negative_prompt")
    }
    parameter_model = create_model(f"Selected_{tool_id.replace('.', '_')}_Parameters", __base__=StrictModel, **fields)
    document = (
        ImageDocument
        if tool_id.startswith("image.generate")
        else EditDocument
        if tool_id.startswith("image.edit")
        else VideoDocument
        if tool_id.startswith("video.")
        else type(None)
    )
    return create_model(
        f"Compose_{tool_id.replace('.', '_')}",
        __base__=CompositionBase,
        tool_id=(Literal[tool_id], tool_id),
        task_mode=(TaskMode, ...),
        parameters=(parameter_model, Field(default_factory=parameter_model)),
        prompt_document=(document | None, None),
    )
