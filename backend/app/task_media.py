"""Batch-resolve the exact inputs and outputs belonging to persisted executions."""

import mimetypes

from sqlalchemy import or_, select, tuple_

from .errors import AppError
from .models import Artifact, Asset, Blob, Plan, ProjectAsset

PURPOSES = {
    "subject_reference": "主体参考",
    "style_reference": "风格参考",
    "scene_reference": "环境参考",
    "composition_reference": "构图参考",
    "first_frame": "开场画面",
    "last_frame": "结束画面",
    "edit_source": "编辑原图",
}


async def resolve_task_media(session, tasks, executions, storage, registry):
    task_map = {task.id: task for task in tasks}
    by_execution, by_task, titles = {}, {}, {}
    if not executions:
        return by_task, by_execution, titles
    execution_ids = [e.id for e in executions]
    artifacts = list(
        await session.scalars(
            select(Artifact).where(Artifact.execution_id.in_(execution_ids)).order_by(Artifact.created_at, Artifact.id)
        )
    )
    asset_ids = {ref["asset_id"] for e in executions for ref in e.inputs}
    asset_ids.update(a.asset_id for a in artifacts if a.asset_id)
    rows = (
        await session.execute(
            select(Asset, Blob)
            .outerjoin(Blob, Blob.sha256 == Asset.blob_id)
            .where(or_(Asset.id.in_(asset_ids), Asset.execution_id.in_(execution_ids)))
            .order_by(Asset.created_at, Asset.id)
        )
    ).all()
    assets = {asset.id: (asset, blob) for asset, blob in rows}
    memberships = {
        (m.project_id, m.asset_id): m
        for m in await session.scalars(
            select(ProjectAsset).where(
                ProjectAsset.asset_id.in_(assets), ProjectAsset.project_id.in_({task.project_id for task in tasks})
            )
        )
    }
    plans = list(
        await session.scalars(
            select(Plan).where(
                tuple_(Plan.id, Plan.revision).in_({(task.plan_id, task.plan_revision) for task in tasks})
            )
        )
    )
    plan_map = {(p.id, p.revision): p for p in plans}
    for task in tasks:
        plan = plan_map.get((task.plan_id, task.plan_revision))
        if plan:
            meta = plan.prompt_metadata or {}
            titles[task.id] = (
                plan.change_summary
                or meta.get("creative_summary_zh")
                or (meta.get("task_goal") or {}).get("description_zh")
                or ""
            )[:180]

    cached = {}

    def asset_view(asset_id, project_id, expected_blob=None):
        key = (asset_id, project_id, expected_blob)
        if key not in cached:
            pair = assets.get(asset_id)
            if not pair:
                cached[key] = dict(
                    asset_id=asset_id,
                    name="素材记录缺失",
                    media_type="unknown",
                    presence="missing",
                    preview_url=None,
                    content_url=None,
                    can_preview=False,
                    can_open=False,
                    project_removed=True,
                    media_info={},
                )
            else:
                asset, blob = pair
                membership = memberships.get((project_id, asset_id))
                changed = bool(expected_blob and expected_blob != asset.blob_id)
                available = bool(
                    not changed
                    and blob
                    and blob.presence == "available"
                    and storage.resolve(blob.relative_path).is_file()
                )
                preview = bool(
                    not changed
                    and blob
                    and blob.presence != "purged"
                    and asset.presence != "purged"
                    and asset.preview_path
                    and storage.resolve("previews/" + asset.preview_path).is_file()
                )
                cached[key] = dict(
                    asset_id=asset.id,
                    name=asset.name,
                    media_type=asset.media_type,
                    media_info=asset.media_info,
                    presence="changed"
                    if changed
                    else "available"
                    if available
                    else "purged"
                    if blob and blob.presence == "purged"
                    else "missing",
                    preview_url=f"/api/v1/assets/{asset.id}/preview" if preview else None,
                    content_url=f"/api/v1/assets/{asset.id}/content" if available else None,
                    can_preview=preview or available,
                    can_open=available,
                    project_removed=not membership or bool(membership.removed_at),
                )
        return dict(cached[key])

    def merge(items):
        result = {}
        for item in items:
            key = (item.get("asset_id") or item["key"], item.get("expected_blob_id"))
            if key not in result:
                result[key] = {**item, "usage_labels": [item["label"]]}
            elif item["label"] not in result[key]["usage_labels"]:
                result[key]["usage_labels"].append(item["label"])
        return list(result.values())

    for e in executions:
        task = task_map[e.task_id]
        configuration = e.configuration or {}
        try:
            tool = registry.get(e.tool_id, e.tool_version)
        except AppError:
            tool = {}
        slot_labels = {item["slot"]: item.get("label", item["slot"]) for item in tool.get("inputs", [])}
        bindings = {
            item.get("slot"): item.get("purpose")
            for item in (configuration.get("prompt_generation") or {}).get("asset_binding_map", [])
        }
        step = dict(
            execution_id=e.id,
            candidate_index=e.candidate_index,
            step_index=configuration.get("step_index"),
            step_label=configuration.get("step_label"),
            is_intermediate=configuration.get("step_count", 1) > 1
            and configuration.get("step_index", 0) < configuration["step_count"] - 1,
        )
        inputs = [
            {
                **asset_view(ref["asset_id"], task.project_id, ref.get("blob_id")),
                **step,
                "key": f"input:{e.id}:{ref['slot']}",
                "slot": ref["slot"],
                "expected_blob_id": ref.get("blob_id"),
                "label": PURPOSES.get(bindings.get(ref["slot"])) or slot_labels.get(ref["slot"], "输入图片"),
            }
            for ref in e.inputs
        ]
        outputs, other = [], []
        records = [a for a in artifacts if a.execution_id == e.id]
        recorded_assets = {a.asset_id for a in records if a.asset_id}
        for a in records:
            if a.representation_of:
                continue
            view = (
                asset_view(a.asset_id, task.project_id)
                if a.asset_id
                else dict(
                    asset_id=None,
                    name=a.reference.get("filename", "待保存产物"),
                    media_type=(mimetypes.guess_type(a.reference.get("filename", ""))[0] or "unknown").split("/")[0],
                    presence="unsaved",
                    preview_url=None,
                    content_url=None,
                    can_preview=False,
                    can_open=False,
                    media_info={},
                    project_removed=False,
                )
            )
            item = {
                **view,
                **step,
                "key": f"artifact:{a.id}",
                "role": a.role,
                "storage_state": a.state,
                "error": a.error,
                "label": "最终产物" if a.role == "final" else "预览输出",
            }
            (outputs if a.role == "final" else other).append(item)
        # Older or restored records may have assets but no artifact rows. Retain their real identity.
        for asset, _ in rows:
            if asset.execution_id == e.id and asset.id not in recorded_assets:
                item = {
                    **asset_view(asset.id, task.project_id),
                    **step,
                    "key": f"asset:{asset.id}",
                    "role": asset.role,
                    "storage_state": "saved",
                    "error": None,
                    "label": "最终产物" if asset.role == "final" else "预览输出",
                }
                (outputs if asset.role == "final" else other).append(item)
        by_execution[e.id] = dict(inputs=merge(inputs), outputs=merge(outputs), other_outputs=merge(other))
        aggregate = by_task.setdefault(task.id, dict(inputs=[], outputs=[], intermediates=[], other_outputs=[]))
        internal = step["is_intermediate"]
        if not configuration.get("depends_on_execution_id"):
            aggregate["inputs"].extend(inputs)
        aggregate["intermediates" if internal else "outputs"].extend(outputs)
        aggregate["other_outputs"].extend(other)
    for media in by_task.values():
        for group in media:
            media[group] = merge(media[group])
    return by_task, by_execution, titles
