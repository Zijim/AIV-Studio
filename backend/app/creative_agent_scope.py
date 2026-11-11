"""One scope predicate, checked before discovery, reading and production submission."""

from sqlalchemy import and_, exists, or_, select

from .errors import AppError
from .models import Asset, CreativeAgentCall, CreativeAgentRun, Execution, ProjectAsset, Task

TERMINAL_RUNS = ("completed", "cancelled", "failed")


def scope_ready(run):
    # Runs created before scope sealing retain their already-established policy.
    return run.scope.get("reference_policy", {}).get("state", "resolved") == "resolved"


def require_scope_ready(run):
    if not scope_ready(run):
        raise AppError(
            "resource_scope_pending",
            "先依据用户原文调用 set_resource_scope 确认素材范围，再读取或提交创作。确认前不开放素材元信息。",
            409,
            {"required_tool": "set_resource_scope"},
        )


def reference_assets(run):
    return or_(
        Asset.id.in_(run.scope.get("selected", [])),
        ProjectAsset.curated_at.is_not(None) if run.scope.get("use_key_assets", True) else False,
    )


def allowed_assets(run, *, protect_pending=False):
    output = exists(
        select(Task.id)
        .join(Execution, Execution.task_id == Task.id)
        .where(Task.agent_run_id == run.id, Execution.id == Asset.execution_id)
    )
    document = exists(
        select(CreativeAgentCall.id).where(CreativeAgentCall.run_id == run.id, CreativeAgentCall.asset_id == Asset.id)
    )
    references = reference_assets(run)
    policy = run.scope.get("reference_policy", {})
    if not protect_pending:
        if not scope_ready(run) or policy.get("mode") == "none":
            references = False
        elif policy.get("mode") == "selected_only":
            references = Asset.id.in_(run.scope.get("selected", []))
        elif policy.get("mode") == "only":
            references = and_(references, Asset.id.in_(policy.get("only_asset_ids", [])))
        references = and_(references, Asset.id.not_in(policy.get("excluded_asset_ids", [])))
    return and_(
        ProjectAsset.project_id == run.project_id,
        ProjectAsset.removed_at.is_(None),
        or_(
            references,
            output,
            document,
        ),
    )


async def scoped_asset(session, run, asset_id):
    require_scope_ready(run)
    asset = await session.scalar(
        select(Asset)
        .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
        .where(allowed_assets(run), Asset.id == asset_id)
    )
    if not asset:
        raise AppError("resource_scope", "素材不在本次创作开放范围，或已被移除。", 403)
    return asset


async def resolve_scope_references(session, run, asset_ids, names, user_quote):
    """Resolve exact user names inside the original ceiling, without listing it."""
    texts = [run.goal, *[entry["answer"] for entry in run.clarifications]]
    if user_quote and not any(user_quote in value for value in texts):
        raise AppError("scope_quote", "素材限制引文必须逐字来自本次用户原文或必要澄清回答。", 409)
    if names and (not user_quote or any(name not in user_quote for name in names)):
        raise AppError("scope_names", "按名称限定素材时，名称必须逐字出现在用户的素材限制引文中。", 409)
    ceiling = and_(
        ProjectAsset.project_id == run.project_id,
        ProjectAsset.removed_at.is_(None),
        reference_assets(run),
    )
    resolved = set(asset_ids)
    for aid in resolved:
        if not await session.scalar(
            select(Asset.id).join(ProjectAsset, ProjectAsset.asset_id == Asset.id).where(ceiling, Asset.id == aid)
        ):
            raise AppError("resource_scope", "限定的素材不在用户原先授权的集合内。", 403)
    for name in names:
        matches = (
            await session.scalars(
                select(Asset.id)
                .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                .where(ceiling, or_(ProjectAsset.display_name == name, Asset.name == name))
            )
        ).all()
        if len(matches) != 1:
            raise AppError("scope_name_ambiguous", "指定名称无法唯一定位到已授权素材，请集中询问用户明确引用。", 409)
        resolved.add(matches[0])
    return sorted(resolved)


async def require_running(session, run_id, version):
    from .plans import require_project

    run = await session.get(CreativeAgentRun, run_id)
    if not run or run.state != "running" or run.control_version != version:
        raise AppError("run_control_changed", "任务已暂停、取消或控制状态改变。", 409)
    await require_project(session, run.project_id, writable=True)
    return run


async def require_submission_inputs(session, task, execution):
    """Recheck at the real generation boundary, after any time in the queue.

    A pause does not retract already accepted tasks. Cancellation and revoked
    resource access do prevent a new GPU submission.
    """
    if not task.agent_run_id:
        return
    run = await session.get(CreativeAgentRun, task.agent_run_id)
    if not run or (run.project_id, run.conversation_id) != (task.project_id, task.conversation_id):
        raise AppError("agent_owner", "生成执行与自主创作归属不一致。", 409)
    if run.state in (*TERMINAL_RUNS, "cancelling"):
        raise AppError("run_cancelled", "自主创作已停止，不再提交新的生成。", 409)
    require_scope_ready(run)
    for ref in execution.inputs:
        await scoped_asset(session, run, ref["asset_id"])


async def protect_active(session, *, project_id=None, conversation_id=None, asset_id=None):
    query = select(CreativeAgentRun).where(CreativeAgentRun.state.not_in(TERMINAL_RUNS))
    if project_id:
        query = query.where(CreativeAgentRun.project_id == project_id)
    if conversation_id:
        query = query.where(CreativeAgentRun.conversation_id == conversation_id)
    runs = (await session.scalars(query)).all()
    for run in runs:
        if asset_id and not await session.scalar(
            select(Asset.id)
            .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
            .where(allowed_assets(run, protect_pending=not scope_ready(run)), Asset.id == asset_id)
        ):
            continue
        raise AppError(
            "agent_run_active",
            "仍有未结束的自主创作，请先完成或取消后再管理此资源。",
            409,
            {"run_id": run.id, "conversation_id": run.conversation_id},
        )
