from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Entity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(default=now)


class Project(Entity, Base):
    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(120))
    goal: Mapped[str] = mapped_column(Text, default="")
    style: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(default=1)
    archived_at: Mapped[str | None]
    removed_at: Mapped[str | None]
    updated_at: Mapped[str] = mapped_column(default=now)


class Conversation(Entity, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_project", "project_id", "updated_at"),
        Index("uq_default_conversation", "project_id", unique=True, sqlite_where=text("is_default = 1")),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(120), default="新对话")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_title: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(default=1)
    archived_at: Mapped[str | None]
    updated_at: Mapped[str] = mapped_column(default=now)


class Blob(Base):
    __tablename__ = "blobs"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    byte_size: Mapped[int]
    relative_path: Mapped[str]
    mime_type: Mapped[str]
    extension: Mapped[str]
    presence: Mapped[str] = mapped_column(default="available")
    created_at: Mapped[str] = mapped_column(default=now)


class Asset(Entity, Base):
    __tablename__ = "assets"
    blob_id: Mapped[str] = mapped_column(ForeignKey("blobs.sha256"))
    name: Mapped[str]
    media_type: Mapped[str]
    origin: Mapped[str]
    role: Mapped[str] = mapped_column(default="imported")
    description: Mapped[str] = mapped_column(Text, default="")
    media_info: Mapped[dict] = mapped_column(JSON, default=dict)
    source_info: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_id: Mapped[str | None] = mapped_column(ForeignKey("executions.id"))
    candidate_index: Mapped[int | None]
    presence: Mapped[str] = mapped_column(default="available")
    preview_path: Mapped[str | None]
    pixel_hash: Mapped[str | None]


class ProjectAsset(Base):
    __tablename__ = "project_assets"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), primary_key=True)
    usage: Mapped[str] = mapped_column(default="material")
    note: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    removed_at: Mapped[str | None]
    revision: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[str] = mapped_column(default=now)


class ContextSnapshot(Entity, Base):
    __tablename__ = "context_snapshots"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class AgentTurn(Entity, Base):
    __tablename__ = "agent_turns"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    state: Mapped[str] = mapped_column(default="planning")
    stage: Mapped[str | None]
    call_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[dict | None] = mapped_column(JSON)
    context_id: Mapped[str | None] = mapped_column(ForeignKey("context_snapshots.id"))


class Message(Entity, Base):
    __tablename__ = "messages"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    __table_args__ = (Index("ix_messages_conversation_history", "conversation_id", "created_at", "id"),)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("agent_turns.id"))
    role: Mapped[str]
    content: Mapped[str] = mapped_column(Text)
    plan_id: Mapped[str | None]
    task_id: Mapped[str | None]
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("agent_turns.id"))
    context_id: Mapped[str | None] = mapped_column(ForeignKey("context_snapshots.id"))
    tool_id: Mapped[str]
    tool_version: Mapped[str]
    inputs: Mapped[list] = mapped_column(JSON)
    parameters: Mapped[dict] = mapped_column(JSON)
    candidate_count: Mapped[int] = mapped_column(default=1)
    seed_list: Mapped[list] = mapped_column(JSON)
    change_summary: Mapped[str] = mapped_column(Text, default="")
    prompt_metadata: Mapped[dict | None] = mapped_column(JSON)
    based_on_execution_id: Mapped[str | None]
    project_revision: Mapped[int]
    plan_hash: Mapped[str]
    state: Mapped[str] = mapped_column(default="draft")
    origin: Mapped[str] = mapped_column(default="manual")
    created_at: Mapped[str] = mapped_column(default=now)


class Task(Entity, Base):
    __tablename__ = "tasks"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    plan_id: Mapped[str]
    plan_revision: Mapped[int]
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("agent_turns.id"))
    origin: Mapped[str]
    queue_seq: Mapped[int] = mapped_column(unique=True)
    state: Mapped[str] = mapped_column(default="queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    candidate_count: Mapped[int]
    best_asset_id: Mapped[str | None]
    retry_of_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    error: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(default=now)
    finished_at: Mapped[str | None]


class Execution(Entity, Base):
    __tablename__ = "executions"
    __table_args__ = (UniqueConstraint("task_id", "candidate_index"),)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    candidate_index: Mapped[int]
    prompt_id: Mapped[str] = mapped_column(unique=True, default=uid)
    client_id: Mapped[str]
    tool_id: Mapped[str]
    tool_version: Mapped[str]
    seed: Mapped[int]
    parameters: Mapped[dict] = mapped_column(JSON)
    inputs: Mapped[list] = mapped_column(JSON)
    execution_state: Mapped[str] = mapped_column(default="pending")
    storage_state: Mapped[str] = mapped_column(default="none")
    submitted_payload: Mapped[dict | None] = mapped_column(JSON)
    executed_payload: Mapped[dict | None] = mapped_column(JSON)
    history: Mapped[dict | None] = mapped_column(JSON)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[str | None]
    finished_at: Mapped[str | None]
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)


class Artifact(Entity, Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("execution_id", "node_id", "field", "item_index"),)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"))
    node_id: Mapped[str]
    field: Mapped[str]
    item_index: Mapped[int]
    role: Mapped[str]
    reference: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(default="pending")
    staging_path: Mapped[str | None]
    saved_blob: Mapped[str | None]
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    representation_of: Mapped[str | None]
    error: Mapped[str | None]


class AssetRelation(Entity, Base):
    __tablename__ = "asset_relations"
    __table_args__ = (UniqueConstraint("from_asset_id", "to_asset_id", "execution_id", "slot"),)
    from_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"))
    to_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"))
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"))
    slot: Mapped[str]
    relation_type: Mapped[str]


class ImportBatch(Entity, Base):
    __tablename__ = "import_batches"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    state: Mapped[str] = mapped_column(default="running")
    items: Mapped[list] = mapped_column(JSON, default=list)


class LLMCall(Entity, Base):
    __tablename__ = "llm_calls"
    turn_id: Mapped[str] = mapped_column(ForeignKey("agent_turns.id"))
    provider: Mapped[str] = mapped_column(default="deepseek_official_responses")
    model: Mapped[str]
    state: Mapped[str] = mapped_column(default="reserved")
    day: Mapped[str]
    reserved_microusd: Mapped[int]
    estimated_microusd: Mapped[int | None]
    usage: Mapped[dict | None] = mapped_column(JSON)
    price_snapshot: Mapped[dict] = mapped_column(JSON)
    provider_request_id: Mapped[str | None]
    response_model: Mapped[str | None]
    request_data: Mapped[dict] = mapped_column(JSON)
    response_data: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None]


class Idempotency(Base):
    __tablename__ = "idempotency"
    key: Mapped[str] = mapped_column(primary_key=True)
    payload_hash: Mapped[str]
    scope: Mapped[str]
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(default=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str]
    project_id: Mapped[str | None]
    entity_id: Mapped[str | None]
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(default=now)


class Maintenance(Entity, Base):
    __tablename__ = "maintenance"
    kind: Mapped[str]
    state: Mapped[str]
    data: Mapped[dict] = mapped_column(JSON)


def record_event(session, kind, project_id=None, entity_id=None, **payload):
    session.add(Event(type=kind, project_id=project_id, entity_id=entity_id, payload=payload))


def row_dict(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}
