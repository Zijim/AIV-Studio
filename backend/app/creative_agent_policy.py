"""Authoritative execution and rework accounting (not monetary accounting)."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import exists, select

from .creative_agent_scope import require_scope_ready
from .errors import AppError
from .models import Asset, CreativeAgentCall, CreativeAgentRun, CreativeAgentStep, Execution, Plan, Task, record_event


def operation_key(run_id, target_id):
    return str(uuid5(NAMESPACE_URL, f"creative-agent-operation:{run_id}:{target_id}"))


def production_operations(run):
    if "production_operations" in run.work_plan:
        return run.work_plan["production_operations"]
    targets = run.work_plan.get("creation_targets")
    if targets is None:  # Already-started v1.0 Runs retain their original identities.
        targets = [
            {**item, "media_type": None, "requirement_ids": [r["id"] for r in run.work_plan.get("requirements", [])]}
            for item in run.work_plan.get("items", [])
        ]
    return [{**target, "operation_key": operation_key(run.id, target["id"])} for target in targets]


def production_operation(run, target_id, media_type=None):
    targets = production_operations(run)
    target = next((item for item in targets if item["id"] == target_id), None)
    if not target:
        raise AppError(
            "operation_identity",
            "operation_id 必须引用已声明的生产目标；计划步骤改名或增加检查步骤不会创建新的生产身份。",
            409,
            {"available_creation_targets": targets},
        )
    if media_type and target.get("media_type") not in (None, media_type):
        raise AppError("operation_type", "创作能力输出类型与该生产目标不一致，请使用对应的稳定目标身份。", 409)
    return target


async def consumed_evidence(session, run, call_id, asset_id, *, media=False):
    call = await session.scalar(
        select(CreativeAgentCall)
        .where(
            CreativeAgentCall.run_id == run.id,
            CreativeAgentCall.call_id == call_id,
            CreativeAgentCall.state == "completed",
        )
        .order_by(CreativeAgentCall.created_at.desc())
        .limit(1)
    )
    if not call or not (call.result or {}).get("ok"):
        candidates = (
            await session.scalars(
                select(CreativeAgentCall).where(
                    CreativeAgentCall.run_id == run.id,
                    CreativeAgentCall.name.in_(("read_resource", "inspect_media")),
                    CreativeAgentCall.state == "completed",
                )
            )
        ).all()
        refs = [c.call_id for c in candidates if (c.result or {}).get("data", {}).get("asset_id") == asset_id]
        raise AppError(
            "evidence_missing",
            "请使用读取/观察结果 data.evidence_call_id 的原值，不能用素材 ID 或自行拼接名称。",
            409,
            {"available_evidence_call_ids_for_this_asset": refs},
        )
    data = call.result.get("data", {})
    if data.get("asset_id") != asset_id or call.name not in (
        ("inspect_media",) if media else ("inspect_media", "read_resource")
    ):
        raise AppError("evidence_mismatch", "证据与素材或检查方式不匹配。", 409)
    asset = await session.get(Asset, asset_id)
    if not asset or data.get("blob_id") != asset.blob_id:
        raise AppError("evidence_stale", "证据的原文件身份已改变。", 409)
    step = await session.get(CreativeAgentStep, call.step_id)
    consumed = await session.scalar(
        select(CreativeAgentStep.id)
        .where(
            CreativeAgentStep.run_id == run.id,
            CreativeAgentStep.sequence > step.sequence,
            CreativeAgentStep.response_data.is_not(None),
            CreativeAgentStep.state.in_(("ready", "completed")),
        )
        .limit(1)
    )
    if not consumed:
        raise AppError("evidence_not_consumed", "应先读取工具返回的真实证据，再提交检查结论。", 409)
    return call, data


async def reserve_execution(session, run, call, count):
    import json

    args = json.loads(call.arguments)
    require_scope_ready(run)
    unknown = await session.scalar(
        select(Task.id).where(Task.agent_run_id == run.id, Task.state == "needs_attention").limit(1)
    )
    if unknown:
        raise AppError(
            "execution_unresolved",
            "本次仍有未知执行或保存问题，请先核对原任务，不能重新生成代替。",
            409,
            {"task_id": unknown},
        )
    if not run.work_plan:
        raise AppError(
            "plan_required",
            "请先声明目标要求、计划和 creation_targets，再提交创作。",
            409,
        )
    plan = await session.get(Plan, (call.plan_id, 1)) if call.plan_id else None
    operation = production_operation(run, args["operation_id"], plan.tool_id.split(".")[0] if plan else None)
    if plan:
        metadata = plan.prompt_metadata or {}
        bound_key = metadata.get("operation_key")
        legacy = metadata.get("guide_version") == "1.0.0"
        if bound_key != operation["operation_key"] and not (legacy and bound_key is None):
            raise AppError("operation_changed", "冻结方案的生产身份与当前声明不一致，请按原操作核对。", 409)
    if run.execution_count + count > run.limits["executions"]:
        raise AppError("execution_limit", "本次创作执行次数已达上限，请保留成果并说明未完成项。", 409)
    # Counter identity is a frozen requirement, independent of problem wording,
    # task, capability, seed and operation/plan names.
    issues = {key: dict(value) for key, value in run.issues.items()}
    requirement_ids = {item["id"] for item in run.work_plan["requirements"]}
    refs = args.get("rework", [])
    if len({r["requirement_id"] for r in refs}) != len(refs):
        raise AppError("rework_duplicate", "返工关联要求不能重复。")
    for ref in refs:
        rid = ref["requirement_id"]
        if rid not in requirement_ids or rid not in operation["requirement_ids"]:
            raise AppError("rework_requirement", "返工必须关联该生产目标绑定的原始要求。")
        _, evidence = await consumed_evidence(session, run, ref["evidence_call_id"], ref["source_asset_id"], media=True)
        source = (await session.execute(
            select(Asset, Task)
            .join(Execution, Execution.id == Asset.execution_id)
            .join(Task, Task.id == Execution.task_id)
            .where(Asset.id == ref["source_asset_id"], Task.agent_run_id == run.id)
        )).first()
        if not source or not evidence.get("frames") or evidence.get("technical", {}).get("decode") == "failed":
            raise AppError("rework_source", "返工必须基于本次真实产物的画面观察，不能基于推测或检测故障。")
        source_call = await session.get(CreativeAgentCall, source[1].agent_call_id)
        source_identity = json.loads(source_call.arguments)["operation_id"] if source_call else None
        if not source_identity or operation_key(run.id, source_identity) != operation["operation_key"]:
            raise AppError("rework_identity", "返工必须沿用原产物所属的生产目标身份，不能借其他产物的证据重开操作。", 409)
        issue = issues.setdefault(rid, {"count": 0, "history": []})
        if issue["count"] + count > run.limits["issue_reworks"]:
            raise AppError("rework_limit", "该要求的问题返工次数已达上限，换工具或描述不能重置。", 409)
        issue["count"] += count
        issue["history"] = [*issue["history"], {**ref, "call_id": call.call_id, "executions": count}]
    # Use the frozen production identity, never the editable display-plan item.
    previous = (
        await session.scalars(
            select(CreativeAgentCall)
            .join(Task, Task.agent_call_id == CreativeAgentCall.id)
            .where(
                Task.agent_run_id == run.id,
                exists(
                    select(Execution.id).where(Execution.task_id == Task.id, Execution.agent_quota_state != "released")
                ),
            )
        )
    ).all()
    if any(
        operation_key(run.id, json.loads(p.arguments)["operation_id"]) == operation["operation_key"] for p in previous
    ) and not refs:
        raise AppError("rework_evidence_required", "该生产目标已执行，再次生成需要关联原问题及真实观察证据。", 409)
    run.issues = issues
    run.work_plan = {
        **run.work_plan, "production_operations": production_operations(run), "operations_frozen": True,
    }
    run.execution_count += count
    run.intent = "creation"


async def reconcile_execution_slots(session, task, executions):
    """Idempotently release only attempts known not to have reached generation.

    Called inside the Worker's state transaction, including startup and user
    cancellation paths. This is execution control, not cost/usage accounting.
    """
    if not task.agent_run_id:
        return
    run = await session.get(CreativeAgentRun, task.agent_run_id)
    call = await session.get(CreativeAgentCall, task.agent_call_id)
    if not run or not call or call.run_id != run.id:
        raise AppError("agent_owner", "生成预约与自主创作调用归属不一致。", 409)
    issues = deepcopy(run.issues)
    for execution in executions:
        if execution.agent_quota_state in (None, "released"):
            continue
        has_submission_evidence = bool(execution.submitted_payload or execution.executed_payload or execution.history)
        has_output = bool(await session.scalar(select(Asset.id).where(Asset.execution_id == execution.id).limit(1)))
        explicitly_rejected = (execution.error or {}).get("code") == "submission_rejected"
        definitely_unsubmitted = (
            execution.agent_quota_state == "reserved"
            and execution.execution_state in ("failed", "cancelled")
            and not has_submission_evidence
            and not has_output
        ) or (
            execution.execution_state == "failed"
            and explicitly_rejected
            and not execution.history
            and not execution.executed_payload
            and not has_output
        )
        if definitely_unsubmitted:
            execution.agent_quota_state = "released"
            run.execution_count = max(0, run.execution_count - 1)
            for ref in (execution.configuration.get("prompt_generation") or {}).get("rework", []):
                issue = issues.get(ref["requirement_id"])
                if issue:
                    issue["count"] = max(0, issue["count"] - 1)
                    for entry in issue["history"]:
                        if entry["call_id"] == call.call_id:
                            entry["released_executions"] = entry.get("released_executions", 0) + 1
            record_event(
                session,
                "agent.updated",
                run.project_id,
                run.id,
                released_execution_id=execution.id,
                reason="definitely_unsubmitted",
            )
        elif (
            has_submission_evidence
            or has_output
            or execution.execution_state
            in (
                "submitting",
                "queued",
                "running",
                "succeeded",
                "unknown",
                "interrupted",
                "submission_unknown",
            )
        ):
            execution.agent_quota_state = "consumed"
    run.issues = issues
