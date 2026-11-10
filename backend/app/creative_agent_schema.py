"""Public workbench tools. Schemas describe actions, not a prescribed workflow."""

from typing import Literal

from pydantic import Field

from .agent_routing import CreativeBrief
from .schemas import InputRef, StrictModel


class StartRun(StrictModel):
    conversation_id: str
    content: str = Field(min_length=1, max_length=16000)
    selected_asset_ids: list[str] = Field(default_factory=list, max_length=100)
    image_roles: dict[str, str] = Field(default_factory=dict)
    use_key_assets: bool = True
    parent_run_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=120)


class RunControl(StrictModel):
    control_version: int = Field(ge=1)


class Answer(RunControl):
    call_id: str
    answer: str = Field(min_length=1, max_length=8000)
    selected_asset_ids: list[str] = Field(default_factory=list, max_length=30)
    idempotency_key: str = Field(min_length=8, max_length=120)


class Context(StrictModel):
    history_offset: int = Field(default=0, ge=0)
    history_limit: int = Field(
        default=0,
        ge=0,
        le=1,
        description="Read one complete prior conversation message on demand. No truncation; old media outputs still need explicit references.",
    )


class Search(StrictModel):
    query: str = Field(default="", max_length=200)
    media_type: Literal["image", "video", "audio", "text"] | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class Read(StrictModel):
    asset_id: str
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=12000, ge=1, le=24000)


class Inspect(StrictModel):
    asset_id: str
    purpose: str = Field(min_length=1, max_length=1000)
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)
    timestamps: list[float] = Field(default_factory=list, max_length=12)
    sample_count: int = Field(default=6, ge=1, le=12)
    crop: list[float] | None = Field(
        default=None, min_length=4, max_length=4, description="Normalized x,y,width,height in [0,1]."
    )
    require_audio: bool = False


class Capabilities(StrictModel):
    category: Literal["image", "generate", "edit", "upscale", "video"] | None = None


class Capability(StrictModel):
    capability_id: str
    version: str | None = None


class Rework(StrictModel):
    requirement_id: str
    source_asset_id: str
    evidence_call_id: str
    problem: str = Field(min_length=1, max_length=1200)
    reason: str = Field(min_length=1, max_length=1200)


class Creation(Capability):
    inputs: list[InputRef] = Field(default_factory=list, max_length=9)
    parameters: dict = Field(default_factory=dict)
    prompt_document: dict | None = None
    purpose: str = Field(min_length=1, max_length=1200)
    operation_id: str = Field(
        min_length=1,
        max_length=80,
        description="Copy an existing update_plan.items[].id exactly (e.g. p2). Not a new operation name. Repeated generation of that item needs rework evidence.",
    )
    candidate_count: int = Field(default=1, ge=1, le=4)
    based_on_asset_id: str | None = None
    rework: list[Rework] = Field(default_factory=list, max_length=12)
    creative_brief: CreativeBrief = Field(default_factory=CreativeBrief)


class Tasks(StrictModel):
    task_ids: list[str] = Field(min_length=1, max_length=30)


class Document(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    format: Literal["markdown", "json"] = "markdown"
    content: str = Field(min_length=1, max_length=64000)
    source_asset_ids: list[str] = Field(default_factory=list, max_length=30)
    parent_asset_id: str | None = None


class Requirement(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1500)


class PlanItem(Requirement):
    state: Literal["pending", "running", "completed"] = "pending"


class WorkPlan(StrictModel):
    output_type: Literal["image", "video", "audio", "text"]
    requirements: list[Requirement] = Field(min_length=1, max_length=30)
    items: list[PlanItem] = Field(min_length=1, max_length=30)
    note: str = Field(default="", max_length=2000)


class Ask(StrictModel):
    question: str = Field(min_length=1, max_length=4000)
    reason: str = Field(min_length=1, max_length=1500)
    options: list[str] = Field(default_factory=list, max_length=6)
    allow_assets: bool = False


class Check(StrictModel):
    requirement_id: str
    asset_id: str
    evidence_call_id: str
    outcome: Literal["passed", "problem", "uncertain"]
    observation: str = Field(min_length=1, max_length=2000)
    coverage: str = Field(min_length=1, max_length=1500)


class Finish(StrictModel):
    kind: Literal["delivered", "reply", "needs_attention"]
    message: str = Field(min_length=1, max_length=8000)
    asset_ids: list[str] = Field(default_factory=list, max_length=30)
    checks: list[Check] = Field(default_factory=list, max_length=60)
    limitations: list[str] = Field(default_factory=list, max_length=30)


TOOL_MODELS = {
    "get_project_context": (
        Context,
        "Read current goal, original constraints and permitted material metadata. Read bounded conversation history on demand; history never grants material access.",
    ),
    "search_resources": (
        Search,
        "Search only explicitly selected assets, user-curated key assets and this Run's outputs.",
    ),
    "read_resource": (Read, "Read an authorized document range or media metadata. Does not observe pixels."),
    "inspect_media": (
        Inspect,
        "Decode authorized media and return timestamped real images, technical checks and coverage. Black/freeze/silence are signals, not failures. No audio semantic judgement.",
    ),
    "list_capabilities": (
        Capabilities,
        "Discover registered production capabilities and actual availability. No editing/TTS/subtitle compositor is connected.",
    ),
    "get_capability": (
        Capability,
        "Read exact version, input slots, prompt document schema, parameter schema, guide and hashes.",
    ),
    "preview_creation": (
        Creation,
        "Optional validation and normalization without generating. Never required before invoke_creation.",
    ),
    "invoke_creation": (
        Creation,
        "Validate, compile, freeze and submit ONE registered creation operation. No user confirmation needed. Rework requires a stable requirement and observed concrete problem, not aesthetic preference.",
    ),
    "get_tasks": (Tasks, "Read actual execution, saving, output and error facts of this Run's tasks."),
    "wait_for_tasks": (
        Tasks,
        "Persistently wait for tasks; exclusive tool batch. Do not repeatedly poll with model calls.",
    ),
    "write_document": (
        Document,
        "Save immutable Markdown/JSON formal deliverable or working document, optionally a new version. Do not save internal reasoning.",
    ),
    "update_plan": (
        WorkPlan,
        "Declare creation boundary, user-goal requirements and visible work plan. No fixed stages; keep requirement IDs stable. Does not execute generation.",
    ),
    "ask_user": (
        Ask,
        "Ask only necessary clarification about original goal and wait. Exclusive batch. No general running-goal modifications.",
    ),
    "finish_run": (
        Finish,
        "Exclusive batch. Deliver only real saved readable assets with checks for every requirement. Critical uncertainty -> needs_attention. Reply only for discussion without creation. No manual acceptance step.",
    ),
}
CONTROL_TOOLS = {"wait_for_tasks", "ask_user", "finish_run"}


def tool_definitions():
    return [
        {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": model.model_json_schema(),
            "strict": False,
        }
        for name, (model, description) in TOOL_MODELS.items()
    ]
