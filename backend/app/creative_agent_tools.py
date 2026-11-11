"""Workbench adapters. Side effects always retain a stable persisted call identity."""

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter
from sqlalchemy import select

from .config import ROOT
from .creative_agent_history import capture_history, history_page
from .creative_agent_media import observe
from .creative_agent_policy import consumed_evidence, operation_key, production_operation, production_operations
from .creative_agent_schema import TOOL_MODELS, AutonomousVideoDocument
from .creative_agent_scope import (
    allowed_assets,
    require_running,
    require_scope_ready,
    resolve_scope_references,
    scoped_asset,
)
from .errors import AppError
from .models import (
    Asset,
    CreativeAgentCall,
    CreativeAgentRun,
    Execution,
    Message,
    Plan,
    ProjectAsset,
    Task,
    now,
    record_event,
    row_dict,
)
from .prompt_compile import COMPILER_VERSION, compile_document
from .prompt_composition import VideoDocument, composition_model
from .prompt_guides import content_hash
from .schemas import ExecutePlan, ManualPlan
from .storage import atomic_json, file_hash

GUIDE_VERSION = "1.1.0"


def creation_document_adapter(tool_id):
    if tool_id.startswith("video.") and tool_id != "video.generate.minimax_ref2v":
        return TypeAdapter(AutonomousVideoDocument | None)
    return TypeAdapter(composition_model(tool_id).model_fields["prompt_document"].annotation)


def success(data=None, *, resources=None, effects=None):
    return {"ok": True, "data": data or {}, "resources": resources or [], "effects": effects or [], "error": None}


def failure(exc):
    return {
        "ok": False,
        "data": {},
        "resources": [],
        "effects": [],
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "stage": "tool",
            "side_effects": False,
            "recovery": "correct_input_or_stop",
        },
    }


class AgentTools:
    def __init__(self, state):
        self.state = state
        self.db, self.storage, self.settings = state.db, state.storage, state.settings
        self.registry, self.plans, self.guides = state.registry, state.plans, state.planner.guides
        folder = ROOT / "prompt_guides" / "autonomous" / GUIDE_VERSION
        self.guide_text = (folder / "guide.md").read_text(encoding="utf-8")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if hashlib.sha256(self.guide_text.encode()).hexdigest() != manifest["sha256"]:
            raise RuntimeError("自主创作指引校验失败，请发布新版本。")
        self.guide_manifest = manifest

    async def dispatch(self, run_id, call, version):
        if call.name not in TOOL_MODELS:
            raise AppError("unknown_tool", "此 Agent 工具不存在。")
        model = TOOL_MODELS[call.name][0]
        args = model.model_validate_json(call.arguments)
        async with self.db.session() as session:
            run = await require_running(session, run_id, version)
        return await getattr(self, call.name)(run, call, args, version)

    async def verified_source(self, session, asset_id):
        asset, blob, path = await self.storage.blob_path(asset_id, session)
        if path.stat().st_size != blob.byte_size or await asyncio.to_thread(file_hash, path) != blob.sha256:
            raise AppError("source_integrity", "素材原文件校验失败，请恢复原文件后再读取或交付。", 409)
        return asset, blob, path

    async def resources(self, session, run, args=None):
        query = (
            select(Asset, ProjectAsset).join(ProjectAsset, ProjectAsset.asset_id == Asset.id).where(allowed_assets(run))
        )
        if args and args.media_type:
            query = query.where(Asset.media_type == args.media_type)
        if args and args.query:
            # Apply the authorization predicate in SQL before pagination/counts.
            query = query.where(
                (Asset.name.contains(args.query, autoescape=True))
                | (ProjectAsset.display_name.contains(args.query, autoescape=True))
                | (ProjectAsset.note.contains(args.query, autoescape=True))
            )
        from sqlalchemy import func

        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await session.execute(
                query.order_by(Asset.created_at.desc(), Asset.id)
                .offset(args.offset if args else 0)
                .limit(args.limit if args else 30)
            )
        ).all()
        tags = await self.storage.tags_for(session, run.project_id, [a.id for a, _ in rows])
        items = [
            {
                "asset_id": a.id,
                "blob_id": a.blob_id,
                "name": m.display_name or a.name,
                "media_type": a.media_type,
                "presence": a.presence,
                "note": m.note,
                "media_info": {k: v for k, v in a.media_info.items() if k != "text_preview"},
                "tags": tags.get(a.id, []),
                "scope_source": "selected"
                if a.id in run.scope.get("selected", [])
                else "key"
                if m.curated_at and run.scope.get("use_key_assets", True)
                else "run_output",
            }
            for a, m in rows
        ]
        return {"items": items, "total": total, "offset": args.offset if args else 0}

    async def get_project_context(self, run, call, args, version):
        async with self.db.session() as session:
            materials = await self.resources(session, run)
            history = await history_page(
                session, run, args.history_offset if args else 0, args.history_limit if args else 0
            )
        return success(
            {
                "goal": run.goal,
                "project": run.goal_snapshot["project"],
                "selected_roles": run.scope.get("image_roles", {}),
                "resource_scope": {
                    "policy": run.scope.get("reference_policy", {"state": "resolved", "mode": "available"}),
                    "explicit_asset_ids": run.scope.get("selected", []),
                    "key_assets_enabled_by_user": run.scope.get("use_key_assets", True),
                    "rule": "Seal scope with set_resource_scope before material access. Existing materials can only be narrowed; Run outputs remain usable.",
                },
                "clarifications": run.clarifications,
                "materials": materials,
                "conversation_history": history,
                "work_plan": run.work_plan,
                "production_operations": production_operations(run),
                "limits": run.limits,
                "used": {
                    "executions": run.execution_count,
                    "model_requests": run.model_count,
                    "tool_calls": run.tool_count,
                    "active_ms": run.active_ms,
                },
                "issues": run.issues,
            }
        )

    async def set_resource_scope(self, run, call, args, version):
        async with self.db.transaction() as session:
            run = await require_running(session, run.id, version)
            old = run.scope.get("reference_policy", {})
            if old.get("call_id") == call.id:
                if old.get("request_hash") != content_hash(args.model_dump()):
                    raise AppError("scope_idempotency_conflict", "同一范围确认调用不能替换参数。", 409)
                return success(old)
            if old.get("state") == "resolved":
                raise AppError("scope_frozen", "本次素材范围已经确认，不能由模型重新扩大或替换。", 409)
            if args.mode != "only" and (args.only_asset_ids or args.only_names):
                raise AppError("scope_mode", "指定 only 素材列表时必须使用 only 模式，不能静默忽略限制。", 409)
            if args.mode == "only" and not (args.only_asset_ids or args.only_names):
                raise AppError("scope_empty", "only 模式必须明确素材 ID 或用户原文中的名称；无素材请使用 none。", 409)
            included = await resolve_scope_references(
                session, run, args.only_asset_ids, args.only_names, args.user_quote
            )
            excluded = await resolve_scope_references(
                session, run, args.exclude_asset_ids, args.exclude_names, args.user_quote
            )
            policy = {
                "state": "resolved", "mode": args.mode,
                "only_asset_ids": [aid for aid in included if aid not in excluded],
                "excluded_asset_ids": excluded, "user_quote": args.user_quote, "call_id": call.id,
                "request_hash": content_hash(args.model_dump()),
            }
            run.scope = {**run.scope, "reference_policy": policy}
            # Recapture permitted discussion only after the material boundary is
            # known. The original goal/project snapshot is otherwise unchanged.
            run.goal_snapshot = {**run.goal_snapshot, "conversation_history": await capture_history(session, run)}
            record_event(session, "agent.updated", run.project_id, run.id)
            return success(policy, effects=[{"type": "resource_scope", "id": run.id}])

    async def search_resources(self, run, call, args, version):
        require_scope_ready(run)
        async with self.db.session() as session:
            return success(await self.resources(session, run, args))

    async def read_resource(self, run, call, args, version):
        async with self.db.session() as session:
            asset = await scoped_asset(session, run, args.asset_id)
            _, blob, path = await self.verified_source(session, asset.id)
            data = {
                "asset_id": asset.id,
                "blob_id": blob.sha256,
                "media_type": asset.media_type,
                "evidence_call_id": call.call_id,
                "media_info": {k: v for k, v in asset.media_info.items() if k != "text_preview"},
            }
        if asset.media_type == "text":
            if blob.byte_size > 2 * 1024 * 1024:
                raise AppError("document_size", "文档超过首版读取上限。")
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            data.update(
                content=text[args.offset : args.offset + args.length],
                offset=args.offset,
                total_characters=len(text),
                omitted=bool(args.offset or len(text) > args.offset + args.length),
            )
        return success(data)

    async def inspect_media(self, run, call, args, version):
        async with self.db.session() as session:
            asset = await scoped_asset(session, run, args.asset_id)
            _, blob, path = await self.verified_source(session, asset.id)
        folder = self.settings.data_dir / "previews" / "agent" / call.id
        data = await asyncio.to_thread(observe, path, asset.media_type, args, folder, self.settings)
        data.update(
            asset_id=asset.id,
            blob_id=blob.sha256,
            purpose=args.purpose,
            evidence_call_id=call.call_id,
            parameters=args.model_dump(),
        )
        return success(
            data,
            resources=[
                {
                    "kind": "observation",
                    "asset_id": asset.id,
                    "blob_id": blob.sha256,
                    "call_id": call.id,
                    "frames": data["frames"],
                }
            ],
        )

    def capability(self, tool):
        guide = self.guides.get(tool["id"], tool["version"])
        doc_schema = creation_document_adapter(tool["id"]).json_schema()
        snapshot = {
            "binding": {"tool_id": tool["id"], "tool_version": tool["version"], "agent_guide": self.guide_manifest},
            "guide": self.guide_text,
            "knowledge_hash": guide["effective_hash"],
            "document_schema": doc_schema,
            "examples": guide["examples"],
        }
        digest = content_hash(snapshot)
        path = self.settings.data_dir / "prompt_guides" / "autonomous" / GUIDE_VERSION / f"{digest}.json"
        if not path.exists():
            atomic_json(path, snapshot)
        from .schemas import PARAMETER_MODELS

        schema = PARAMETER_MODELS[tool["id"]].model_json_schema()
        schema["properties"] = {
            k: v for k, v in schema["properties"].items() if k not in ("prompt", "instruction", "negative_prompt")
        }
        schema["required"] = [k for k in schema.get("required", []) if k in schema["properties"]]
        return {
            "capability_id": tool["id"],
            "version": tool["version"],
            "name": tool["name"],
            "category": tool["category"],
            "inputs": tool["inputs"],
            "availability": tool["availability"],
            "parameter_schema": schema,
            "prompt_document_schema": {key: value for key, value in doc_schema.items() if key != "$defs"},
            "$defs": doc_schema.get("$defs", {}),
            "guide": self.guide_text,
            "examples": guide["examples"],
            "description": guide["description"],
            "limitations": guide["limitations"],
            "guide_hash": digest,
            "template_hash": tool["template_hash"],
            "parameter_schema_hash": content_hash(schema),
            "compiler_version": COMPILER_VERSION,
            "resource_limits": {
                key: getattr(self.settings, key)
                for key in ("image_max_pixels", "video_max_pixels", "video_max_frames", "upscale_max_edge")
            },
        }

    async def list_capabilities(self, run, call, args, version):
        cards = self.guides.cards(self.registry, self.settings)
        return success(
            {
                "items": [
                    {
                        k: c[k]
                        for k in (
                            "id",
                            "version",
                            "name",
                            "category",
                            "availability",
                            "assistant_available",
                            "description",
                            "limitations",
                            "inputs",
                        )
                    }
                    for c in cards
                    if not args.category
                    or c["category"] == args.category
                    or (args.category == "image" and c["id"].startswith("image."))
                ],
                "unconnected": ["editing/composition", "standalone voiceover", "mixing", "subtitle composition"],
            }
        )

    async def get_capability(self, run, call, args, version):
        return success(self.capability(self.registry.get(args.capability_id, args.version)))

    async def prepare_creation(self, run, args):
        require_scope_ready(run)
        tool = self.registry.get(args.capability_id, args.version)
        capability = self.capability(tool)
        if any(k in args.parameters for k in ("prompt", "instruction", "negative_prompt")):
            raise AppError("prompt_authority", "正文只由 prompt_document 编译，不能同时写入 parameters。")
        if len({r.slot for r in args.inputs}) != len(args.inputs):
            raise AppError("input_slots", "输入槽位不能重复。")
        refs = sorted(
            args.inputs, key=lambda r: next((i for i, s in enumerate(tool["inputs"]) if s["slot"] == r.slot), 99)
        )
        async with self.db.session() as session:
            assets = {}
            for ref in refs:
                asset = await scoped_asset(session, run, ref.asset_id)
                await self.verified_source(session, asset.id)
                assets[ref.slot] = row_dict(asset)
            source_params = {}
            if args.based_on_asset_id:
                source = await scoped_asset(session, run, args.based_on_asset_id)
                execution = await session.get(Execution, source.execution_id) if source.execution_id else None
                if execution and execution.tool_id == tool["id"]:
                    source_params = dict(execution.parameters)
        params = {**source_params, **args.parameters}
        for key in (
            "prompt",
            "instruction",
            "negative_prompt",
            "resolved_frames",
            "resolved_seconds",
            "resolution",
            "max_resolution",
        ):
            params.pop(key, None)
        if tool["category"] == "video":
            params.pop("width", None)
            params.pop("height", None)
        doc = creation_document_adapter(tool["id"]).validate_python(args.prompt_document)
        if isinstance(doc, AutonomousVideoDocument) and doc.style_description:
            document = doc.model_dump(exclude={"style_description"})
            first_shot = document["shots"][0]
            first_shot["description"] = f"{doc.style_description.strip()}\n{first_shot['description']}"
            doc = VideoDocument.model_validate(document)
        # Normalize once with placeholder text to obtain deterministic dimensions.
        placeholder = (
            {}
            if tool["category"] == "upscale"
            else {"instruction" if tool["category"] == "edit" else "prompt": "pending compilation"}
        )
        normalized, seeds, dimensions = self.registry.normalize(
            tool["id"], {**params, **placeholder}, assets, self.settings, args.candidate_count, version=tool["version"]
        )
        bindings = [
            {
                "slot": ref.slot,
                "asset_id": ref.asset_id,
                "blob_id": assets[ref.slot]["blob_id"],
                "label": f"<Picture {i}>" if tool["category"] == "video" else None,
            }
            for i, ref in enumerate(refs, 1)
        ]
        values, sections = compile_document(tool["id"], doc, args.creative_brief, normalized, bindings, autonomous=True)
        normalized.update(values)
        if tool["category"] == "video":
            for key in ("width", "height", "resolved_frames", "resolved_seconds"):
                normalized.pop(key, None)
        if tool["category"] == "upscale":
            for key in ("resolution", "max_resolution"):
                normalized.pop(key, None)
        normalized, seeds, dimensions = self.registry.normalize(
            tool["id"], normalized, assets, self.settings, args.candidate_count, version=tool["version"]
        )
        metadata = {
            "generation_origin": "autonomous",
            "validation_status": "structure_validated",
            "agent_run_id": run.id,
            "operation_id": args.operation_id,
            "goal_snapshot_hash": content_hash(run.goal_snapshot),
            "purpose": args.purpose,
            "rework": [r.model_dump() for r in args.rework],
            "guide_id": "autonomous",
            "guide_version": GUIDE_VERSION,
            "effective_guide_hash": capability["guide_hash"],
            "parameter_schema_hash": capability["parameter_schema_hash"],
            "template_hash": tool["template_hash"],
            "compiler_version": COMPILER_VERSION,
            "prompt_document": args.prompt_document,
            "sections": sections,
            "asset_binding_map": bindings,
            "creative_brief": args.creative_brief.model_dump(),
            "parameter_sources": {
                k: "explicit_or_designed"
                if k in args.parameters
                else "inherited"
                if k in source_params
                else "default_or_resolved"
                for k in normalized
            },
            "compiled_prompt_sha256": hashlib.sha256(
                (normalized.get("prompt") or normalized.get("instruction") or "").encode()
            ).hexdigest(),
        }
        if run.work_plan:
            target = production_operation(run, args.operation_id, tool["id"].split(".")[0])
            metadata.update(operation_key=target["operation_key"], target_requirement_ids=target["requirement_ids"])
        return tool, normalized, refs, metadata, dimensions

    async def preview_creation(self, run, call, args, version):
        tool, params, refs, metadata, dimensions = await self.prepare_creation(run, args)
        if args.parameters.get("seed") is None and metadata["parameter_sources"].get("seed") != "inherited":
            params["seed"] = None
        return success(
            {
                "submitted": False,
                "capability_id": tool["id"],
                "version": tool["version"],
                "resolved": params,
                "dimensions": dimensions,
                "unresolved": ["seed"] if params.get("seed") is None else [],
                "guide_hash": metadata["effective_guide_hash"],
            }
        )

    async def invoke_creation(self, run, call, args, version):
        require_scope_ready(run)
        async with self.db.session() as session:
            existing = await session.scalar(select(Task).where(Task.agent_call_id == call.id))
            if existing:
                return await self.creation_result(session, existing)
            old_plan = await session.get(Plan, (call.plan_id, 1)) if call.plan_id else None
        production_operation(run, args.operation_id)
        if old_plan:
            plan = row_dict(old_plan)
        else:
            tool, params, refs, metadata, _ = await self.prepare_creation(run, args)
            plan = await self.plans.create(
                run.project_id,
                ManualPlan(
                    tool_id=tool["id"],
                    inputs=refs,
                    parameters=params,
                    candidate_count=args.candidate_count,
                    change_summary=args.purpose,
                    conversation_id=run.conversation_id,
                ),
                origin="autonomous",
                agent_run_id=run.id,
                agent_call_id=call.id,
                control_version=version,
                tool_version=tool["version"],
                prompt_metadata=metadata,
            )
        result = await self.plans.execute(
            plan["id"],
            ExecutePlan(
                revision=plan["revision"],
                plan_hash=plan["plan_hash"],
                idempotency_key="agent-execute:" + call.id,
                accept_previous_context=True,
            ),
            agent_call_id=call.id,
            control_version=version,
        )
        self.state.worker.wake.set()
        async with self.db.session() as session:
            return await self.creation_result(session, await session.get(Task, result["task_id"]))

    async def creation_result(self, session, task):
        plan = await session.get(Plan, (task.plan_id, task.plan_revision))
        return success(
            {
                "submitted": True,
                "task_id": task.id,
                "state": task.state,
                "plan_id": plan.id,
                "plan_hash": plan.plan_hash,
                "tool_version": plan.tool_version,
                "parameters": plan.parameters,
                "seed_list": plan.seed_list,
                "metadata": plan.prompt_metadata,
            },
            effects=[{"type": "task", "id": task.id}],
        )

    async def task_facts(self, session, run, ids):
        items = []
        for task_id in dict.fromkeys(ids):
            task = await session.get(Task, task_id)
            if not task or task.agent_run_id != run.id:
                raise AppError("task_scope", "只能查询或等待本次创作拥有的任务。", 403)
            executions = (await session.scalars(select(Execution).where(Execution.task_id == task.id))).all()
            assets = (
                await session.scalars(
                    select(Asset)
                    .join(Execution, Asset.execution_id == Execution.id)
                    .where(Execution.task_id == task.id, Asset.role != "preview")
                )
            ).all()
            permitted = []
            for asset in assets:
                try:
                    await scoped_asset(session, run, asset.id)
                    permitted.append({"asset_id": asset.id, "blob_id": asset.blob_id, "media_type": asset.media_type})
                except AppError:
                    continue
            items.append(
                {
                    "task_id": task.id,
                    "state": task.state,
                    "error": task.error,
                    "outputs": permitted,
                    "executions": [
                        {
                            "id": e.id,
                            "state": e.execution_state,
                            "storage_state": e.storage_state,
                            "quota_state": e.agent_quota_state,
                            "error": e.error,
                        }
                        for e in executions
                    ],
                }
            )
        return items

    async def get_tasks(self, run, call, args, version):
        async with self.db.session() as session:
            return success({"items": await self.task_facts(session, run, args.task_ids)})

    async def wait_for_tasks(self, run, call, args, version):
        from .plans import ACTIVE_TASKS

        async with self.db.transaction() as session:
            run = await require_running(session, run.id, version)
            items = await self.task_facts(session, run, args.task_ids)
            if all(item["state"] not in ACTIVE_TASKS for item in items):
                return success({"items": items})
            run.state = "waiting_task"
            return None

    async def write_document(self, run, call, args, version):
        async with self.db.transaction() as session:
            await require_running(session, run.id, version)
            for aid in [*args.source_asset_ids, *([args.parent_asset_id] if args.parent_asset_id else [])]:
                await scoped_asset(session, run, aid)
        if args.format == "json":
            json.loads(args.content)
        suffix = ".json" if args.format == "json" else ".md"
        name = Path(args.name).name
        if not name.lower().endswith(suffix):
            name += suffix
        stable_id = str(uuid5(NAMESPACE_URL, "aiv-agent-document:" + call.id))
        staged = self.settings.data_dir / "staging" / (call.id + ".document")
        await asyncio.to_thread(staged.write_text, args.content, encoding="utf-8")
        aid = await self.storage.ingest(
            staged,
            name,
            run.project_id,
            origin="agent_document",
            role="document",
            stable_asset_id=stable_id,
            source_info={
                "agent_run_id": run.id,
                "agent_call_id": call.id,
                "parent_asset_id": args.parent_asset_id,
                "source_asset_ids": args.source_asset_ids,
                "author": "agent",
                "format": args.format,
            },
        )
        async with self.db.transaction() as session:
            stored = await session.get(CreativeAgentCall, call.id)
            stored.asset_id = aid
            current = await session.get(CreativeAgentRun, run.id)
            current.intent = "creation"
            asset = await session.get(Asset, aid)
            return success(
                {"asset_id": aid, "blob_id": asset.blob_id, "parent_asset_id": args.parent_asset_id},
                effects=[{"type": "document", "id": aid}],
            )

    async def update_plan(self, run, call, args, version):
        if len({r.id for r in args.requirements}) != len(args.requirements) or len({i.id for i in args.items}) != len(
            args.items
        ):
            raise AppError("plan_ids", "要求和计划项标识不能重复。")
        requirements = {requirement.id for requirement in args.requirements}
        async with self.db.transaction() as session:
            run = await require_running(session, run.id, version)
            wrote_document = await session.scalar(
                select(CreativeAgentCall.id)
                .where(CreativeAgentCall.run_id == run.id, CreativeAgentCall.asset_id.is_not(None))
                .limit(1)
            )
            if (run.execution_count or wrote_document) and (
                run.work_plan.get("requirements") != [r.model_dump() for r in args.requirements]
                or run.work_plan.get("output_type") != args.output_type
            ):
                raise AppError("goal_frozen", "已执行创作后不能更换目标要求、标识或输出类型。", 409)
            old_operations = production_operations(run)
            plan = args.model_dump()
            if "creation_targets" in args.model_fields_set:
                proposed = [
                    {**target, "operation_key": operation_key(run.id, target["id"])}
                    for target in plan["creation_targets"]
                ]
            else:
                plan["creation_targets"] = run.work_plan.get("creation_targets", [])
                proposed = old_operations
            target_ids = {target["id"] for target in proposed}
            if len(target_ids) != len(proposed) or any(
                len(set(target["requirement_ids"])) != len(target["requirement_ids"])
                or not set(target["requirement_ids"]).issubset(requirements)
                for target in proposed
            ):
                raise AppError("creation_targets", "生产目标身份必须唯一，且只能绑定已声明的目标要求。", 409)
            locked = run.work_plan.get("operations_frozen", False) or bool(await session.scalar(
                select(Task.id).where(Task.agent_run_id == run.id).limit(1)
            ))
            if locked:
                # Compatibility for v1.0 plans, which had no separate targets.
                if not proposed and any(item.get("media_type") is None for item in old_operations):
                    proposed = old_operations
                old_by_id = {item["id"]: item for item in old_operations}
                if set(old_by_id) != {item["id"] for item in proposed} or any(
                    old_by_id[item["id"]].get("media_type") not in (None, item.get("media_type"))
                    or set(old_by_id[item["id"]]["requirement_ids"]) != set(item["requirement_ids"])
                    or old_by_id[item["id"]]["text"] != item["text"]
                    for item in proposed if item["id"] in old_by_id
                ):
                    raise AppError(
                        "creation_targets_frozen",
                        "生产已经开始，不能新增、改名或重绑生产目标以取得新执行身份。可自由调整计划步骤、说明、工具和参数；重做沿用原目标并关联返工证据。",
                        409,
                        {"existing_creation_targets": old_operations},
                    )
            run.work_plan = {
                **plan, "revision": run.work_plan.get("revision", 0) + 1,
                "production_operations": proposed, "operations_frozen": locked,
            }
            run.intent = "creation"
            record_event(session, "agent.updated", run.project_id, run.id)
            return success(run.work_plan)

    async def ask_user(self, run, call, args, version):
        async with self.db.transaction() as session:
            run = await require_running(session, run.id, version)
            run.state = "waiting_user"
            self.message(
                session,
                run,
                args.question,
                {
                    "agent_run_id": run.id,
                    "question_call_id": call.call_id,
                    "reason": args.reason,
                    "options": args.options,
                },
            )
        return None

    @staticmethod
    def message(session, run, text, details=None):
        session.add(
            Message(
                project_id=run.project_id,
                conversation_id=run.conversation_id,
                role="assistant",
                content=text,
                details=details or {"agent_run_id": run.id},
            )
        )
        record_event(session, "message.created", run.project_id, run.id)

    async def finish_run(self, run, call, args, version):
        from .plans import ACTIVE_TASKS

        async with self.db.transaction() as session:
            run = await require_running(session, run.id, version)
            if args.kind == "reply" and run.intent == "creation":
                raise AppError("delivery_required", "已开始创作，不能改标普通回复跳过交付核验。", 409)
            if args.kind == "delivered":
                if not args.asset_ids or not run.work_plan:
                    raise AppError("delivery_missing", "交付必须包含目标计划和真实成果。", 409)
                tasks = (await session.scalars(select(Task).where(Task.agent_run_id == run.id))).all()
                if any(t.state in (*ACTIVE_TASKS, "needs_attention") for t in tasks):
                    raise AppError("delivery_tasks", "仍有未结束执行、未知提交或保存待处理的任务。", 409)
                unresolved = await session.scalar(
                    select(CreativeAgentCall.id)
                    .where(
                        CreativeAgentCall.run_id == run.id,
                        CreativeAgentCall.id != call.id,
                        CreativeAgentCall.state.not_in(("completed", "failed")),
                    )
                    .limit(1)
                )
                if unresolved:
                    raise AppError("delivery_calls", "仍有未闭合调用或必要询问。", 409)
                assets = {}
                for aid in args.asset_ids:
                    asset = await scoped_asset(session, run, aid)
                    await self.verified_source(session, aid)
                    assets[aid] = asset
                if not any(a.media_type == run.work_plan["output_type"] for a in assets.values()):
                    raise AppError("delivery_type", "阶段素材类型不能代替目标成品类型。", 409)
                requirements = {r["id"] for r in run.work_plan["requirements"]}
                if {c.requirement_id for c in args.checks} != requirements or any(
                    c.outcome != "passed" for c in args.checks
                ):
                    raise AppError(
                        "delivery_checks", "每项目标要求都需要有依据的通过结论；明确问题或关键不确定不能完整交付。", 409
                    )
                if set(assets) - {check.asset_id for check in args.checks}:
                    raise AppError("delivery_unchecked", "交付清单中仍有未检查的成果，请逐份核对。", 409)
                for check in args.checks:
                    if check.asset_id not in assets:
                        raise AppError("delivery_evidence", "检查必须对应交付清单中的实际成果。", 409)
                    asset = assets[check.asset_id]
                    _, evidence = await consumed_evidence(
                        session,
                        run,
                        check.evidence_call_id,
                        asset.id,
                        media=asset.media_type in ("image", "video", "audio"),
                    )
                    if asset.media_type in ("image", "video", "audio"):
                        if evidence.get("technical", {}).get("decode") != "complete":
                            raise AppError("delivery_decode", "媒体未完成解码检查，不能声明已完整检查。", 409)
                        if evidence.get("technical", {}).get("required_audio_present") is False:
                            raise AppError("delivery_audio", "必要音轨缺失，不能标记完整交付。", 409)
                        if asset.media_type in ("image", "video") and not evidence.get("frames"):
                            raise AppError("delivery_pixels", "媒体缺少实际画面观察。", 409)
                    elif evidence.get("omitted"):
                        await self.check_document_coverage(session, run, asset)
                run.deliverables = list(dict.fromkeys(args.asset_ids))
            run.completion = args.model_dump()
            run.state = "needs_attention" if args.kind == "needs_attention" else "completed"
            run.finished_at = now() if run.state == "completed" else None
            run.stop_reason = (
                {"code": "agent_report", "message": args.message} if run.state == "needs_attention" else None
            )
            self.message(
                session,
                run,
                args.message,
                {
                    "agent_run_id": run.id,
                    "kind": args.kind,
                    "asset_ids": args.asset_ids,
                    "checks": [c.model_dump() for c in args.checks],
                    "limitations": args.limitations,
                },
            )
            record_event(session, "agent.updated", run.project_id, run.id)
            return success({"state": run.state, "kind": args.kind, "asset_ids": args.asset_ids})

    async def check_document_coverage(self, session, run, asset):
        reads = (
            await session.scalars(
                select(CreativeAgentCall).where(
                    CreativeAgentCall.run_id == run.id,
                    CreativeAgentCall.name == "read_resource",
                    CreativeAgentCall.state == "completed",
                )
            )
        ).all()
        intervals, total = [], None
        for call in reads:
            data = (call.result or {}).get("data", {})
            if data.get("asset_id") != asset.id or data.get("blob_id") != asset.blob_id:
                continue
            try:
                await consumed_evidence(session, run, call.call_id, asset.id)
            except AppError:
                continue
            total = data["total_characters"]
            intervals.append((data["offset"], data["offset"] + len(data.get("content", ""))))
        through = 0
        for start, end in sorted(intervals):
            if start > through:
                break
            through = max(through, end)
        if total is None or through < total:
            raise AppError(
                "delivery_document",
                "文档只读了部分内容，请分段完整核对后交付。",
                409,
                {"read_through": through, "total_characters": total},
            )
