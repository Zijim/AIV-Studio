"""One scope predicate, checked before discovery, reading and production submission."""

from sqlalchemy import and_, exists, or_, select

from .errors import AppError
from .models import Asset, CreativeAgentCall, CreativeAgentRun, Execution, ProjectAsset, Task

TERMINAL_RUNS = ("completed", "cancelled", "failed")


def allowed_assets(run):
    output = exists(
        select(Task.id)
        .join(Execution, Execution.task_id == Task.id)
        .where(Task.agent_run_id == run.id, Execution.id == Asset.execution_id)
    )
    document = exists(
        select(CreativeAgentCall.id).where(CreativeAgentCall.run_id == run.id, CreativeAgentCall.asset_id == Asset.id)
    )
    return and_(
        ProjectAsset.project_id == run.project_id,
        ProjectAsset.removed_at.is_(None),
        or_(
            Asset.id.in_(run.scope.get("selected", [])),
            ProjectAsset.curated_at.is_not(None) if run.scope.get("use_key_assets", True) else False,
            output,
            document,
        ),
    )


async def scoped_asset(session, run, asset_id):
    asset = await session.scalar(
        select(Asset)
        .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
        .where(allowed_assets(run), Asset.id == asset_id)
    )
    if not asset:
        raise AppError("resource_scope", "素材不在本次创作开放范围，或已被移除。", 403)
    return asset


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
            .where(allowed_assets(run), Asset.id == asset_id)
        ):
            continue
        raise AppError(
            "agent_run_active",
            "仍有未结束的自主创作，请先完成或取消后再管理此资源。",
            409,
            {"run_id": run.id, "conversation_id": run.conversation_id},
        )
