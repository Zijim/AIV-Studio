"""Conversation ownership and compatibility for the original project-level API."""

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update

from .errors import AppError
from .models import AgentTurn, ContextSnapshot, Conversation, Message, Plan, Setting, Task, now, record_event


def default_conversation_id(project_id):
    return str(uuid5(NAMESPACE_URL, "aiv:default-conversation:" + project_id))


def memory_key(conversation_id):
    return "conversation_memory:" + conversation_id


async def resolve_conversation(session, project_id, conversation_id=None, writable=False):
    if conversation_id:
        conversation = await session.get(Conversation, conversation_id)
    else:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.project_id == project_id, Conversation.is_default.is_(True))
        )
        if not conversation:
            conversation = Conversation(
                id=default_conversation_id(project_id),
                project_id=project_id,
                title="默认对话",
                is_default=True,
                auto_title=False,
            )
            session.add(conversation)
            await session.flush()
            # Compatibility for integrations which still insert legacy rows directly.
            for model in (Message, AgentTurn, ContextSnapshot, Plan, Task):
                await session.execute(
                    update(model)
                    .where(model.project_id == project_id, model.conversation_id.is_(None))
                    .values(conversation_id=conversation.id)
                )
            previous = await session.get(Setting, "project_memory:" + project_id)
            if previous:
                session.add(Setting(key=memory_key(conversation.id), value=dict(previous.value)))
    if not conversation or conversation.project_id != project_id:
        raise AppError("conversation_not_found", "对话不属于当前项目或不存在。", 404)
    if writable and conversation.archived_at:
        raise AppError("conversation_archived", "此对话已归档，请先恢复或开启新对话。", 409)
    return conversation


async def touch_conversation(session, conversation_id):
    conversation = await session.get(Conversation, conversation_id) if conversation_id else None
    if conversation:
        conversation.updated_at = now()
        record_event(session, "conversation.updated", conversation.project_id, conversation.id)
