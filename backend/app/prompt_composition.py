"""Typed documents for the selected tool, separate from executable parameters."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, create_model

from .agent_routing import BASE_SYSTEM, CreativeBrief, FrameAssessment, TaskMode, VisualObservation
from .schemas import PARAMETER_MODELS, ImageAssetTitle, StrictModel

COMPOSITION_SYSTEM = (
    BASE_SYSTEM
    + """
本阶段为已经固定的工具生成一次方案，只使用selected_tool及专用guide，不自行切换工具。原始用户需求优先于摘要；必要信息缺失时kind=clarify；只讨论时kind=reply。
kind=propose时填写prompt_document、parameters、中文创作摘要和检查项。参数不能包含由后台计算的width/height/resolved_frames等派生视频值；resolved_execution_settings给出冻结值。图像生成宽高按自身Schema提供，视频派生值由后台核算。
图片工具的提案同时填写Schema中的asset_title，给成品一个简短的名字。
route.creative_brief是后台已经确认的创作约束，会由后台完整继承，不需要重复抄写。可省略输出creative_brief；如填写，只补充确有依据的必要细节，不得修改已确定的镜头、声音、原文或执行参数。后续工具的deferred_parameters不属于本次执行参数。保守补充并明确assumptions_zh。
shot_intent仅从输入route.creative_brief读取，不在输出顶层重复；输出只填写shot_review。单镜头对象严格为{"index":1,"cut_at_seconds":null,"description":"完整镜头正文"}。镜头运动、构图和动作全部写入description，不另设camera、description_note、cut_at_seconds_note或placeholder等字段。
prompt_document按当前工具Schema书写，模型正文默认英文，原文台词/歌词/画面文字保持原语言、措辞和标点。说明、约束摘要和review为中文。
同工具小改保留未改变的来源参数和种子。seed_policy=inherit继承明确来源、new探索新结果、explicit使用用户明确种子；跨工具不声称种子等价。
每张本阶段附图都返回一项真实visual_observations。只使用asset_binding_map内的生成模型引用标签；辅助图像不拥有额外的Picture编号。
constraint_review只写简短的保持项、已落实修改、必要补充及冲突，不写思维链。不把模板示例的剧情、对白、人物和音乐抄进当前作品。
constraint_review.conflicts只列当前方案仍无法解决、需要用户取舍的要求冲突，此时必须kind=clarify。原图缺地面、需要扩图或调整姿态，若已由本次instruction安排修改，应记入changed，不属于未解决冲突。
所有对象只输出Schema中已有字段；镜头对象只能有index、cut_at_seconds、description，不能新增note、description_note等字段，空字段也不例外。
message_zh、change_summary_zh、creative_summary_zh、constraint_review只描述作品和创作决定。修复Schema字段或格式是内部步骤，不向用户叙述“删除字段、修复JSON”等实现细节。
shot_intent是当前镜头目标，逐项落实起始、主要变化、结束和场景。准备首帧时只画动作开始前的姿态与完整环境，不把结束姿态画成首帧；视频中固定机位也应完整表现主要动作，不用呼吸/微小晃动替代动作。
所有视频propose填写shot_review确认主要动作、状态衔接及场景已写入实际正文。仅真实开场画面模式video_i2va/video_fl2va填写frame_assessment；文生、参考、尾帧及尚未生成的内部开场画面不做首帧像素评估。首帧准备是静态图片，用start_frame_prepared检查起始画面。参考图缺背景、地面、道具或起始姿态不构成冲突，直接在Ref2VA中生成。场景与接触材料需物理相容。
shot_review只检查本次prompt_document是否包含相应要求。start_frame_prepared=true表示instruction已描述完整起始画面，不表示图片已经生成或验收；不能因为尚未执行就填false。frame_assessment则描述当前实际输入图，编辑方案可以修正其中的问题。
关键帧效果风险写入方案供查看，不询问直接视频或准备首帧。尚未生成的planned_input只提供计划中的画面说明，不能当作实际像素；附图仍是原始参考，visual_observations只描述它们。真正缺少必要素材或创作要求冲突时才clarify。
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


class SubjectDefinition(StrictModel):
    label: str = Field(pattern=r"^<Subject [1-9][0-9]*>$")
    description: str = Field(min_length=1, max_length=2500)


class ReferenceVideoDocument(VideoDocument):
    subject_definitions: list[SubjectDefinition] = Field(min_length=1, max_length=20)
    summary: str = Field(min_length=1, max_length=2000)
    retention_analysis: list[str] = Field(min_length=1, max_length=30)
    style_description: str = Field(min_length=1, max_length=1500)


class ConstraintReview(StrictModel):
    kept: list[str] = Field(default_factory=list, max_length=20)
    changed: list[str] = Field(default_factory=list, max_length=20)
    conflicts: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="当前方案仍未解决、必须用户取舍的要求冲突。指令已安排的扩图/补地面/改姿态归入changed。非空应clarify。",
    )


class ShotReview(StrictModel):
    main_action_present: bool = False
    start_to_end_consistent: bool = False
    scene_addressed: bool = False
    start_frame_prepared: bool | None = Field(
        default=None,
        description="首帧编辑instruction已包含完整起始姿态、环境和动作空间即为true；不是已经生成或画面验收通过。",
    )
    notes_zh: list[str] = Field(default_factory=list, max_length=10)


class CompositionBase(StrictModel):
    kind: Literal["propose", "clarify", "reply"]
    message_zh: str = Field(min_length=1, max_length=4000)
    change_summary_zh: str = Field(default="", max_length=4000)
    selection_reason_zh: str = Field(default="", max_length=1000)
    assumptions_zh: list[str] = Field(default_factory=list, max_length=12)
    creative_summary_zh: str = Field(default="", max_length=2000)
    creative_brief: CreativeBrief | None = None
    constraint_review: ConstraintReview = Field(default_factory=ConstraintReview)
    visual_observations: list[VisualObservation] = Field(default_factory=list, max_length=12)
    seed_policy: Literal["inherit", "new", "explicit"] = "inherit"
    candidate_count: int = Field(default=1, ge=1, le=4)
    frame_assessment: FrameAssessment | None = None
    shot_review: ShotReview | None = None


@lru_cache(maxsize=16)
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
        else ReferenceVideoDocument
        if tool_id == "video.generate.minimax_ref2v"
        else VideoDocument
        if tool_id.startswith("video.")
        else type(None)
    )
    image_fields = {"asset_title": (ImageAssetTitle, None)} if tool_id.startswith("image.") else {}
    return create_model(
        f"Compose_{tool_id.replace('.', '_')}",
        __base__=CompositionBase,
        tool_id=(Literal[tool_id], tool_id),
        task_mode=(TaskMode, ...),
        parameters=(parameter_model, Field(default_factory=parameter_model)),
        prompt_document=(document | None, None),
        **image_fields,
    )
