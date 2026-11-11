"""Bounded conversation memory without carrying revoked material descriptions."""

import json

from sqlalchemy import select

from .creative_agent_scope import scoped_asset
from .errors import AppError
from .models import CreativeAgentCall, CreativeAgentRun, CreativeAgentStep, Message


def resource_ids(value):
    ids = set()
    if isinstance(value, dict):
        if isinstance(value.get("asset_id"), str):
            ids.add(value["asset_id"])
        for child in value.values():
            ids.update(resource_ids(child))
    elif isinstance(value, list):
        for child in value:
            ids.update(resource_ids(child))
    return ids


def request_resource_ids(request):
    """Our first input is structured context JSON, never arbitrary user JSON."""
    if not isinstance(request, dict):
        return set()
    items = request.get("input") or []
    if (
        not isinstance(items, list)
        or not items
        or not isinstance(items[0], dict)
        or not isinstance(items[0].get("content"), str)
    ):
        return set()
    try:
        initial = json.loads(items[0]["content"])
    except (ValueError, TypeError):
        return set()
    return resource_ids(initial) if isinstance(initial, dict) and "current" in initial else set()


async def capture_history(session, run):
    messages = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == run.conversation_id, Message.role.in_(("user", "assistant")))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(16)
        )
    ).all()
    history, refs_by_run = [], {}
    for message in reversed(messages):
        if (message.details or {}).get("agent_run_id") == run.id:
            continue
        refs = set()
        if message.role == "assistant":
            details = message.details or {}
            old_id = details.get("agent_run_id")
            old_run = await session.get(CreativeAgentRun, old_id) if old_id else None
            if (
                not old_run
                or old_run.conversation_id != run.conversation_id
                or (old_run.completion or {}).get("kind") != "reply"
            ):
                continue
            if old_id not in refs_by_run:
                seen = set()
                for request in (
                    await session.scalars(
                        select(CreativeAgentStep.request_data).where(CreativeAgentStep.run_id == old_id)
                    )
                ).all():
                    seen.update(request_resource_ids(request))
                for result in (
                    await session.scalars(select(CreativeAgentCall.result).where(CreativeAgentCall.run_id == old_id))
                ).all():
                    seen.update(resource_ids(result))
                refs_by_run[old_id] = seen
            refs = refs_by_run[old_id]
            try:
                for aid in refs:
                    await scoped_asset(session, run, aid)
            except AppError:
                continue
        history.append(
            {
                "message_id": message.id,
                "role": message.role,
                "content": message.content,
                "resources": [{"asset_id": aid} for aid in sorted(refs)],
            }
        )
    return history


def snapshot_history(run):
    if "conversation_history" in run.goal_snapshot:
        return run.goal_snapshot["conversation_history"]
    return [
        {"role": "user", "content": text, "resources": []} for text in run.goal_snapshot.get("prior_user_messages", [])
    ]


async def history_page(session, run, offset=0, limit=1):
    history = snapshot_history(run)
    items = history[offset : offset + limit] if limit else []
    for item in items:
        for aid in resource_ids(item):
            await scoped_asset(session, run, aid)
    return {
        "total": len(history),
        "offset": offset,
        "items": items,
        "read_more": "get_project_context(history_offset=..., history_limit=1)",
    }
