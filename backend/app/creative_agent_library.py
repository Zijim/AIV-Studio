"""Keep verified deliveries in the project library and recycle unused outputs."""

from sqlalchemy import func, or_, select

from .creative_agent_scope import document_assets, output_assets
from .models import Asset, CreativeAgentRun, Execution, Plan, Project, ProjectAsset, Setting, Task, now
from .plans import ACTIVE_TASKS

HISTORY_SORT_KEY = "creative_library_history_v1"


def completed_deliveries():
    return select(CreativeAgentRun).where(
        CreativeAgentRun.state == "completed",
        CreativeAgentRun.completion["kind"].as_string() == "delivered",
        func.json_array_length(CreativeAgentRun.deliverables) > 0,
    )


async def delivered_asset_ids(session, project_id):
    rows = await session.scalars(
        completed_deliveries()
        .where(CreativeAgentRun.project_id == project_id)
        .with_only_columns(CreativeAgentRun.deliverables)
    )
    return {asset_id for asset_ids in rows for asset_id in asset_ids}


async def recycle_unselected_assets(session, project_id, *, run_id=None):
    run_ids = completed_deliveries().where(CreativeAgentRun.project_id == project_id)
    if run_id is not None:
        run_ids = run_ids.where(CreativeAgentRun.id == run_id)
    run_ids = run_ids.with_only_columns(CreativeAgentRun.id)
    best_ids = select(Task.best_asset_id).where(Task.project_id == project_id, Task.best_asset_id.is_not(None))
    memberships = (
        await session.scalars(
            select(ProjectAsset)
            .join(Asset, Asset.id == ProjectAsset.asset_id)
            .where(
                ProjectAsset.project_id == project_id,
                ProjectAsset.removed_at.is_(None),
                ProjectAsset.curated_at.is_(None),
                ProjectAsset.asset_id.not_in(best_ids),
                # A renamed, edited or previously restored membership is a user choice.
                ProjectAsset.revision == 1,
                ProjectAsset.display_name.is_(None),
                ProjectAsset.note == "",
                Asset.origin != "imported",
                Asset.role != "preview",
                or_(output_assets(run_ids), document_assets(run_ids)),
            )
        )
    ).all()
    if not memberships:
        return []

    protected = await delivered_asset_ids(session, project_id)
    scopes = await session.scalars(
        select(CreativeAgentRun.scope).where(CreativeAgentRun.project_id == project_id)
    )
    protected.update(asset_id for scope in scopes for asset_id in scope.get("selected", []))
    input_lists = await session.scalars(
        select(Plan.inputs)
        .where(Plan.project_id == project_id, Plan.state == "draft")
        .union_all(
            select(Execution.inputs)
            .join(Task, Task.id == Execution.task_id)
            .where(
                Task.project_id == project_id,
                or_(Task.agent_run_id.is_(None), Task.state.in_((*ACTIVE_TASKS, "needs_attention"))),
            )
        )
    )
    protected.update(ref["asset_id"] for inputs in input_lists for ref in inputs if ref.get("asset_id"))

    removed_at = now()
    recycled = []
    for membership in memberships:
        if membership.asset_id not in protected and not membership.tags:
            membership.removed_at = removed_at
            membership.revision += 1
            recycled.append(membership.asset_id)
    return recycled


async def organize_existing_creations(db):
    async with db.transaction() as session:
        if await session.get(Setting, HISTORY_SORT_KEY):
            return
        project_ids = await session.scalars(
            select(Project.id).where(
                Project.removed_at.is_(None),
                Project.archived_at.is_(None),
                Project.id.in_(completed_deliveries().with_only_columns(CreativeAgentRun.project_id)),
            )
        )
        for project_id in project_ids:
            await recycle_unselected_assets(session, project_id)
        session.add(Setting(key=HISTORY_SORT_KEY, value={"completed_at": now()}))
