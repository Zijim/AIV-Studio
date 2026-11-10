from datetime import datetime

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from .conversations import memory_key, resolve_conversation
from .creative_agent_scope import TERMINAL_RUNS, protect_active
from .errors import AppError
from .models import (
    AgentTurn,
    Conversation,
    CreativeAgentRun,
    Idempotency,
    Message,
    Setting,
    Task,
    now,
    record_event,
    row_dict,
)
from .plans import ACTIVE_TASKS, digest, require_project
from .schemas import ConversationArchive, ConversationCreate, ConversationRename

router = APIRouter(prefix="/api/v1/projects/{project_id}/conversations")


@router.get("")
async def conversations(request: Request, project_id: str):
    state = request.app.state
    async with state.db.transaction() as session:
        await require_project(session, project_id)
        await resolve_conversation(session, project_id)
        latest = (
            select(Message.content)
            .where(Message.conversation_id == Conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        count = select(func.count(Message.id)).where(Message.conversation_id == Conversation.id).scalar_subquery()
        latest_time = (
            select(func.max(Message.created_at)).where(Message.conversation_id == Conversation.id).scalar_subquery()
        )
        rows = (
            await session.execute(
                select(Conversation, latest, count, latest_time)
                .where(Conversation.project_id == project_id)
                .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            )
        ).all()
        active_turns = (
            await session.scalars(
                select(AgentTurn).where(
                    AgentTurn.project_id == project_id, AgentTurn.state.in_(("planning", "executing"))
                )
            )
        ).all()
        active_tasks = (
            await session.scalars(select(Task).where(Task.project_id == project_id, Task.state.in_(ACTIVE_TASKS)))
        ).all()
        active_runs = (await session.scalars(select(CreativeAgentRun).where(
            CreativeAgentRun.project_id == project_id, CreativeAgentRun.state.not_in(TERMINAL_RUNS)))).all()
        memories = {
            item.key: item.value
            for item in (
                await session.scalars(select(Setting).where(Setting.key.in_([memory_key(c.id) for c, _, _, _ in rows])))
            ).all()
        }
        items = [
            {
                **row_dict(c),
                "updated_at": max(
                    c.updated_at, last_time or c.updated_at, key=lambda value: datetime.fromisoformat(value).timestamp()
                ),
                "preview": " ".join((preview or "").split())[:140],
                "message_count": count,
                "planning": any(t.conversation_id == c.id and t.state == "planning" for t in active_turns),
                "running": any(t.conversation_id == c.id for t in active_tasks),
                "agent_run": next(({"id": r.id, "state": r.state} for r in active_runs if r.conversation_id == c.id), None),
                "summarized_message_count": memories.get(memory_key(c.id), {}).get("message_count", 0),
            }
            for c, preview, count, last_time in rows
        ]
        items.sort(key=lambda item: datetime.fromisoformat(item["updated_at"]).timestamp(), reverse=True)
    return {
        "items": items,
        "context_limits": {
            "max_input_tokens": state.settings.max_input_tokens,
            "summary_threshold_tokens": state.settings.history_summary_threshold_tokens,
            "recent_tokens": state.settings.history_recent_tokens,
            "max_output_tokens": state.settings.max_output_tokens,
        },
    }


@router.post("", status_code=201)
async def create_conversation(request: Request, project_id: str, body: ConversationCreate):
    title = body.title.strip() if body.title is not None else "新对话"
    if not title:
        raise AppError("conversation_title", "对话名称不能为空。")
    hashed = digest({"project_id": project_id, "title": body.title, "mode": body.mode})
    async with request.app.state.db.transaction() as session:
        await require_project(session, project_id, writable=True)
        if body.idempotency_key:
            old = await session.get(Idempotency, body.idempotency_key)
            if old:
                if old.scope != "conversation_create" or old.payload_hash != hashed:
                    raise AppError("idempotency_conflict", "新建对话的提交标识冲突。", 409)
                return old.result
        conversation = Conversation(project_id=project_id, title=title, mode=body.mode, auto_title=body.title is None)
        session.add(conversation)
        await session.flush()
        result = row_dict(conversation)
        if body.idempotency_key:
            session.add(
                Idempotency(key=body.idempotency_key, scope="conversation_create", payload_hash=hashed, result=result)
            )
        record_event(session, "conversation.updated", project_id, conversation.id)
        return result


@router.patch("/{conversation_id}")
async def rename_conversation(request: Request, project_id: str, conversation_id: str, body: ConversationRename):
    title = body.title.strip()
    if not title:
        raise AppError("conversation_title", "对话名称不能为空。")
    async with request.app.state.db.transaction() as session:
        await require_project(session, project_id, writable=True)
        conversation = await resolve_conversation(session, project_id, conversation_id)
        if conversation.revision != body.revision:
            raise AppError("revision_conflict", "对话已更新，请刷新后重试。", 409)
        conversation.title, conversation.auto_title = title, False
        conversation.revision += 1
        conversation.updated_at = now()
        record_event(session, "conversation.updated", project_id, conversation.id)
        return row_dict(conversation)


@router.post("/{conversation_id}/archive")
async def archive_conversation(request: Request, project_id: str, conversation_id: str, body: ConversationArchive):
    async with request.app.state.db.transaction() as session:
        await require_project(session, project_id, writable=True)
        conversation = await resolve_conversation(session, project_id, conversation_id)
        if conversation.revision != body.revision:
            raise AppError("revision_conflict", "对话已更新，请刷新后重试。", 409)
        if body.archived:
            await protect_active(session, conversation_id=conversation.id)
            planning = await session.scalar(
                select(AgentTurn.id)
                .where(AgentTurn.conversation_id == conversation.id, AgentTurn.state.in_(("planning", "executing")))
                .limit(1)
            )
            running = await session.scalar(
                select(Task.id).where(Task.conversation_id == conversation.id, Task.state.in_(ACTIVE_TASKS)).limit(1)
            )
            if planning or running:
                raise AppError("conversation_busy", "此对话仍在规划或执行，请等待结束或取消后再归档。", 409)
        conversation.archived_at = now() if body.archived else None
        conversation.revision += 1
        conversation.updated_at = now()
        record_event(session, "conversation.updated", project_id, conversation.id)
        return row_dict(conversation)
