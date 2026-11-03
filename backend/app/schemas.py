from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(default="", max_length=8000)
    style: str = Field(default="", max_length=8000)


class ProjectUpdate(StrictModel):
    revision: int
    name: str | None = Field(default=None, min_length=1, max_length=120)
    goal: str | None = Field(default=None, max_length=8000)
    style: str | None = Field(default=None, max_length=8000)


class ConversationCreate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)


class ConversationRename(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    revision: int = Field(ge=1)


class ConversationArchive(StrictModel):
    archived: bool
    revision: int = Field(ge=1)


class InputRef(StrictModel):
    slot: str
    asset_id: str


class ManualPlan(StrictModel):
    tool_id: str
    inputs: list[InputRef] = Field(default_factory=list)
    parameters: dict = Field(default_factory=dict)
    candidate_count: int = Field(default=1, ge=1, le=4)
    change_summary: str = Field(default="", max_length=4000)
    based_on_execution_id: str | None = None
    conversation_id: str | None = None


class PlanPatch(ManualPlan):
    revision: int
    accept_previous_context: bool = False
    prompt_mode: Literal["guided", "manual"] | None = None


class ExecutePlan(StrictModel):
    revision: int
    plan_hash: str
    idempotency_key: str = Field(min_length=8, max_length=120)
    accept_previous_context: bool = False


class GenerateParams(StrictModel):
    prompt: str = Field(min_length=1, max_length=12000)
    negative_prompt: str = Field(default="", max_length=8000)
    width: int = Field(default=1024, ge=512, le=1536)
    height: int = Field(default=1024, ge=512, le=1536)
    seed: int | None = Field(default=None, ge=0, le=9007199254740991)


class QwenGenerateParams(GenerateParams):
    preset: Literal["fast", "quality"] = "fast"


class EditParams(StrictModel):
    instruction: str = Field(min_length=1, max_length=12000)
    negative_prompt: str = Field(default="", max_length=8000)
    output_width: int | None = Field(default=None, ge=64, le=1280)
    output_height: int | None = Field(default=None, ge=64, le=1280)
    seed: int | None = Field(default=None, ge=0, le=9007199254740991)


class ReferenceParams(StrictModel):
    instruction: str = Field(min_length=1, max_length=12000)
    negative_prompt: str = Field(default="", max_length=8000)
    seed: int | None = Field(default=None, ge=0, le=9007199254740991)


class UpscaleParams(StrictModel):
    scale: float = Field(default=2, ge=1, le=4)
    seed: int | None = Field(default=None, ge=0, le=4294967295)


class VideoParams(StrictModel):
    prompt: str = Field(min_length=1, max_length=12000)
    duration_seconds: float = Field(default=5, ge=1, le=10)
    megapixels: float = Field(default=0.4, ge=0.1, le=0.6)
    aspect_ratio: Literal["source", "16:9", "9:16", "1:1"] = "source"
    seed: int | None = Field(default=None, ge=0, le=9007199254740991)


class TextVideoParams(VideoParams):
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"


class ReferenceVideoParams(TextVideoParams):
    ref_image_size: Literal["match", "max"] = "match"


PARAMETER_MODELS = {
    "image.generate.zimage": GenerateParams,
    "image.generate.qwen": QwenGenerateParams,
    "image.edit.qwen.fast": EditParams,
    "image.edit.qwen.reference": ReferenceParams,
    "image.upscale.seedvr2": UpscaleParams,
    "video.generate.minimax_i2v": VideoParams,
    "video.generate.minimax_t2v": TextVideoParams,
    "video.generate.minimax_ref2v": ReferenceVideoParams,
    "video.generate.minimax_fl2v": VideoParams,
    "video.generate.minimax_l2v": VideoParams,
}


class AssetNote(StrictModel):
    name: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=12000)
    usage: Literal["material", "reference", "output"] | None = None
    # Legacy name-based tags: omitted means "leave tags unchanged". The asset
    # tag endpoint is the authoritative way to edit normalized tags.
    tags: list[str] | None = Field(default=None, max_length=30)
    revision: int | None = None


class KeyAssetsRequest(StrictModel):
    asset_ids: list[str] = Field(min_length=1, max_length=200)
    tag_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("素材不能重复")
        if len(set(self.tag_ids)) != len(self.tag_ids):
            raise ValueError("标签不能重复")
        return self


class KeyAssetsRemove(StrictModel):
    asset_ids: list[str] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("素材不能重复")
        return self


class TagCreate(StrictModel):
    name: str = Field(min_length=1, max_length=24)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class TagPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=24)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    position: int | None = Field(default=None, ge=0, le=10000)

    @model_validator(mode="after")
    def has_change(self):
        if self.name is None and self.color is None and self.position is None:
            raise ValueError("没有需要修改的字段")
        return self


class AssetTags(StrictModel):
    tag_ids: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.tag_ids)) != len(self.tag_ids):
            raise ValueError("标签不能重复")
        return self


class TagAssets(StrictModel):
    asset_ids: list[str] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("素材不能重复")
        return self


class RevisePlan(StrictModel):
    plan_id: str
    revision: int = Field(ge=1)
    rewrite_only: bool = False


class WorkflowChoice(StrictModel):
    goal_id: str
    action: Literal["prepare_first_frame", "direct_video", "continue_video"]
    asset_id: str | None = None


class MessageRequest(StrictModel):
    content: str = Field(min_length=1, max_length=16000)
    selected_asset_ids: list[str] = Field(default_factory=list, max_length=30)
    image_roles: dict[
        str,
        Literal[
            "auto",
            "edit_source",
            "subject_reference",
            "style_reference",
            "scene_reference",
            "composition_reference",
            "first_frame",
            "last_frame",
        ],
    ] = Field(default_factory=dict, max_length=30)
    idempotency_key: str = Field(min_length=8, max_length=120)
    conversation_id: str | None = None
    preferred_tool_id: str | None = None
    revise_plan: RevisePlan | None = None
    workflow_choice: WorkflowChoice | None = None

    @model_validator(mode="after")
    def selected_roles(self):
        if len(set(self.selected_asset_ids)) != len(self.selected_asset_ids):
            raise ValueError("所选素材不能重复")
        if not self.image_roles.keys() <= set(self.selected_asset_ids):
            raise ValueError("图片用途只能设置给本轮选中的素材")
        if sum(role == "first_frame" for role in self.image_roles.values()) > 1:
            raise ValueError("一次只能指定一张视频首帧")
        if sum(role == "last_frame" for role in self.image_roles.values()) > 1:
            raise ValueError("一次只能指定一张视频尾帧")
        return self
