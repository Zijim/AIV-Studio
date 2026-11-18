"""Workbench adapters. Side effects always retain a stable persisted call identity."""

import asyncio
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter
from sqlalchemy import func, select

from .agent_routing import LiteralText
from .composition import CompositionError, compose_timeline, save_frame, validate_timeline
from .config import ROOT
from .creative_agent_history import capture_history, history_page
from .creative_agent_library import recycle_unselected_assets
from .creative_agent_media import observe
from .creative_agent_policy import consumed_evidence, operation_key, production_operation, production_operations
from .creative_agent_schema import TOOL_MODELS, AutonomousVideoDocument
from .creative_agent_scope import (
    allowed_assets,
    background_status,
    require_run_background,
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
from .schemas import LOCAL_EDIT_REGION_FIELDS, LOCAL_EDIT_TOOL, ExecutePlan, ManualPlan
from .storage import atomic_json, file_hash

GUIDE_VERSION = "1.6.0"
QWEN_QUALITY_MENTION = re.compile(r"(?<!\d)50\s*(?:步|steps?\b)|质量(?:档|模式|预设)|\bquality\b", re.I)
REQUIREMENT_PUNCTUATION = re.compile(r"[\s，,。.、；;：:！!？?（）()【】\[\]《》<>“”\"'‘’…—\-_/\\]+")


def requirement_key(text):
    """Compare meaning-preserving text, not incidental punctuation or spacing."""
    return REQUIREMENT_PUNCTUATION.sub("", unicodedata.normalize("NFKC", text or ""))


def requirement_changes(existing, submitted):
    old = {item["id"]: item for item in existing}
    new = {item["id"]: item for item in submitted}
    changes = []
    for item_id in sorted(set(old) | set(new)):
        if item_id not in old or item_id not in new:
            changes.append(
                {
                    "id": item_id,
                    "existing": (old.get(item_id) or {}).get("text"),
                    "submitted": (new.get(item_id) or {}).get("text"),
                }
            )
        elif requirement_key(old[item_id].get("text")) != requirement_key(new[item_id].get("text")):
            changes.append(
                {"id": item_id, "existing": old[item_id].get("text"), "submitted": new[item_id].get("text")}
            )
    return changes


def observation_key(parameters):
    """The observation-affecting request, excluding the free-text purpose."""
    payload = {key: value for key, value in (parameters or {}).items() if key != "purpose"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


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

    async def observed(self, session, run, asset_id, blob_id):
        """A clip may only enter composition after a real decoded observation exists."""
        calls = (
            await session.scalars(
                select(CreativeAgentCall).where(
                    CreativeAgentCall.run_id == run.id,
                    CreativeAgentCall.name == "inspect_media",
                    CreativeAgentCall.state == "completed",
                )
            )
        ).all()
        for item in calls:
            data = (item.result or {}).get("data") or {}
            if data.get("asset_id") == asset_id and data.get("blob_id") == blob_id:
                return True
        return False

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
            from .plans import require_project

            project = await require_project(session, run.project_id)
            materials = await self.resources(session, run)
            history = await history_page(
                session, run, args.history_offset if args else 0, args.history_limit if args else 0
            )
        return success(
            {
                "goal": run.goal,
                "project": run.goal_snapshot["project"],
                "project_background": background_status(run, project),
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
                "dialogue_locks": run.dialogue_locks,
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
        key = observation_key(args.model_dump(exclude={"purpose"}))
        async with self.db.session() as session:
            asset = await scoped_asset(session, run, args.asset_id)
            _, blob, path = await self.verified_source(session, asset.id)
            prior = None
            prior_data = {}
            candidates = (
                await session.scalars(
                    select(CreativeAgentCall)
                    .where(
                        CreativeAgentCall.run_id == run.id,
                        CreativeAgentCall.name == "inspect_media",
                        CreativeAgentCall.state == "completed",
                    )
                    .order_by(CreativeAgentCall.created_at)
                    .limit(50)
                )
            ).all()
            for candidate in candidates:
                data = (candidate.result or {}).get("data") or {}
                if data.get("duplicate") or data.get("asset_id") != asset.id:
                    continue
                if data.get("blob_id") != blob.sha256:
                    continue
                previous_key = data.get("observation_key") or observation_key(data.get("parameters") or {})
                if previous_key == key:
                    prior, prior_data = candidate, data
                    break
        if prior:
            return success(
                {
                    "duplicate": True,
                    "duplicate_of": prior.call_id,
                    "evidence_call_id": prior.call_id,
                    "observation_key": key,
                    "asset_id": asset.id,
                    "blob_id": blob.sha256,
                    "purpose": args.purpose,
                    "parameters": args.model_dump(),
                    "technical": prior_data.get("technical"),
                    "frames": [],
                    "hint": (
                        "本次观察与已有观察参数完全相同，没有产生新证据。"
                        "请改变裁剪、时间点或采样区间；也可以直接使用 evidence_call_id 所指的已有观察判断或交付。"
                        "重复相同观察会被计为无进展。"
                    ),
                },
                resources=[
                    {
                        "kind": "observation",
                        "asset_id": asset.id,
                        "blob_id": blob.sha256,
                        "call_id": prior.id,
                        "frames": [],
                    }
                ],
            )
        folder = self.settings.data_dir / "previews" / "agent" / call.id
        data = await asyncio.to_thread(observe, path, asset.media_type, args, folder, self.settings)
        data.update(
            asset_id=asset.id,
            blob_id=blob.sha256,
            purpose=args.purpose,
            evidence_call_id=call.call_id,
            observation_key=key,
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
                "unconnected": ["standalone voiceover", "mixing", "subtitle composition"],
            }
        )

    async def get_capability(self, run, call, args, version):
        return success(self.capability(self.registry.get(args.capability_id, args.version)))

    async def dialogue_brief(self, run, args):
        brief = args.creative_brief
        target_id = args.operation_id
        locked = run.dialogue_locks.get(target_id, [])
        sources = (run.goal_snapshot.get("original", run.goal), *(item["answer"] for item in run.clarifications))
        declared = [
            item for item in brief.literal_texts
            if item.kind in ("dialogue", "lyrics") and item.text.strip() and any(item.text in text for text in sources)
        ]
        if declared:
            # Commit before compiling so a rejected preview or submission retains the user's exact wording.
            async with self.db.transaction() as session:
                current = await require_running(session, run.id, run.control_version)
                locked = current.dialogue_locks.get(target_id, [])
                locked_texts = {item["text"] for item in locked}
                additions = [item.model_dump() for item in declared if item.text not in locked_texts]
                if additions:
                    locked = [*locked, *additions]
                    current.dialogue_locks = {**current.dialogue_locks, target_id: locked}
        if not locked:
            return brief
        locked_texts = {item["text"] for item in locked}
        literals = [item for item in brief.literal_texts if item.kind == "visible_text" or item.text not in locked_texts]
        literals.extend(LiteralText.model_validate(item) for item in locked)
        return brief.model_copy(update={"literal_texts": literals})

    async def prepare_creation(self, run, args):
        require_scope_ready(run)
        async with self.db.session() as session:
            await require_run_background(session, run)
        tool = self.registry.get(args.capability_id, args.version)
        capability = self.capability(tool)
        target = production_operation(run, args.operation_id, tool["id"].split(".")[0]) if run.work_plan else None
        brief = await self.dialogue_brief(run, args) if tool["category"] == "video" else args.creative_brief
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
        parameters = dict(args.parameters)
        if tool["id"] == LOCAL_EDIT_TOOL:
            for key in LOCAL_EDIT_REGION_FIELDS:
                source_params.pop(key, None)
        if tool["id"] == "image.generate.qwen":
            user_texts = (run.goal, *(entry["answer"] for entry in run.clarifications))
            requested_quality = args.creative_brief.requested_parameters.get("preset") == "quality" and any(
                QWEN_QUALITY_MENTION.search(value) for value in user_texts
            )
            if requested_quality and parameters.get("preset", "fast") in ("fast", "quality"):
                parameters["preset"] = "quality"
            else:
                # A model choice or an inherited preset should use the schema default.
                for values in (source_params, parameters):
                    if values.get("preset") == "quality":
                        del values["preset"]
        params = {**source_params, **parameters}
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
        values, sections = compile_document(tool["id"], doc, brief, normalized, bindings, autonomous=True)
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
            "creative_brief": brief.model_dump(),
            "parameter_sources": {
                k: "explicit_or_designed"
                if k in parameters
                else "inherited"
                if k in source_params
                else "default_or_resolved"
                for k in normalized
            },
            "compiled_prompt_sha256": hashlib.sha256(
                (normalized.get("prompt") or normalized.get("instruction") or "").encode()
            ).hexdigest(),
        }
        if target:
            metadata.update(operation_key=target["operation_key"], target_requirement_ids=target["requirement_ids"])
            if tool["id"].startswith("image.") and target.get("asset_title"):
                metadata["asset_title"] = target["asset_title"]
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
            # A task that was already submitted stays valid; only a new submission
            # must still match the goal/style frozen when the Run started.
            await require_run_background(session, run)
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

    async def compose_video(self, run, call, args, version):
        if not run.work_plan:
            raise AppError("plan_required", "请先声明目标计划与成片目标，再合成。", 409)
        async with self.db.session() as session:
            clips = []
            for ref in args.clips:
                asset = await scoped_asset(session, run, ref.asset_id)
                if asset.media_type != "video":
                    raise AppError(
                        "compose_source_type", "只有视频素材可以参与合成。", 409, {"asset_id": ref.asset_id}
                    )
                _, blob, path = await self.verified_source(session, asset.id)
                if not await self.observed(session, run, asset.id, blob.sha256):
                    raise AppError(
                        "compose_source_unchecked",
                        "参与合成的镜头必须先完成一次真实画面观察。",
                        409,
                        {"asset_id": ref.asset_id},
                    )
                clips.append(
                    {
                        "asset_id": asset.id,
                        "path": path,
                        "blob_id": blob.sha256,
                        "in_frame": ref.in_frame,
                        "frames": ref.frames,
                        "label": ref.label,
                    }
                )
            used = await session.scalar(
                select(func.count())
                .select_from(CreativeAgentCall)
                .where(
                    CreativeAgentCall.run_id == run.id,
                    CreativeAgentCall.name == "compose_video",
                    CreativeAgentCall.id != call.id,
                )
            )
        limit = run.limits.get("composes", self.settings.agent_max_composes)
        if used >= limit:
            raise AppError(
                "compose_limit",
                "本次创作的合成次数已达上限，请保留已完成部分并说明未完成项。",
                409,
                {"limit": limit, "used": used},
            )
        if args.target_frames is not None and args.target_frames > self.settings.agent_compose_max_frames:
            raise AppError(
                "compose_frames",
                "目标帧数超过允许的合成上限。",
                409,
                {"target_frames": args.target_frames, "limit": self.settings.agent_compose_max_frames},
            )
        production_operation(run, args.operation_id, "video")
        if args.dry_run:
            try:
                timeline = validate_timeline(clips, target_frames=args.target_frames)
            except CompositionError as exc:
                raise AppError(exc.code, exc.message, 409, exc.details) from exc
            return success(
                {
                    "dry_run": True,
                    "natural": args.target_frames is None,
                    "frames": timeline["frames"],
                    "duration": timeline["frames"] / timeline["fps"],
                    "width": timeline["width"],
                    "height": timeline["height"],
                    "fps": timeline["fps"],
                    "clips": [
                        {
                            "asset_id": item["asset_id"],
                            "in_frame": item["in_frame"],
                            "frames": item["frames"],
                            "available_frames": item["probe"]["frames"],
                        }
                        for item in timeline["clips"]
                    ],
                }
            )
        staged = self.settings.data_dir / "staging" / (call.id + ".mp4")
        try:
            result = await asyncio.to_thread(compose_timeline, clips, staged, target_frames=args.target_frames)
            name = Path(args.output_name).name
            if not name.lower().endswith(".mp4"):
                name += ".mp4"
            aid = await self.storage.ingest(
                staged,
                name,
                run.project_id,
                origin="composition",
                role="final",
                description=f"成片：{result['frames']} 帧 / {result['duration']:.3f} 秒",
                source_info={
                    "composition": {
                        "version": "1.0.0",
                        "agent_run_id": run.id,
                        "agent_call_id": call.id,
                        "operation_id": args.operation_id,
                        "fps": result["fps"],
                        "width": result["width"],
                        "height": result["height"],
                        "frames": result["frames"],
                        "duration": result["duration"],
                        "audio": "native",
                        "clips": result["clips"],
                    }
                },
                stable_asset_id=str(uuid5(NAMESPACE_URL, "aiv-agent-composition:" + call.id)),
            )
        except CompositionError as exc:
            raise AppError(exc.code, exc.message, 409, exc.details) from exc
        finally:
            staged.unlink(missing_ok=True)
            staged.with_suffix(".json").unlink(missing_ok=True)
        async with self.db.transaction() as session:
            stored = await session.get(CreativeAgentCall, call.id)
            stored.asset_id = aid
            current = await session.get(CreativeAgentRun, run.id)
            current.intent = "creation"
            asset = await session.get(Asset, aid)
            data = {
                "asset_id": aid,
                "blob_id": asset.blob_id,
                "natural": args.target_frames is None,
                "frames": result["frames"],
                "duration": result["duration"],
                "width": result["width"],
                "height": result["height"],
                "fps": result["fps"],
                "clips": result["clips"],
            }
        return success(
            data,
            resources=[{"kind": "composition", "asset_id": aid, "blob_id": data["blob_id"]}],
            effects=[{"type": "asset", "id": aid}],
        )

    async def extract_frame(self, run, call, args, version):
        async with self.db.session() as session:
            asset = await scoped_asset(session, run, args.asset_id)
            if asset.media_type != "video":
                raise AppError("frame_source_type", "只能从视频素材抽帧。", 409, {"asset_id": args.asset_id})
            _, blob, path = await self.verified_source(session, asset.id)
            source_name = asset.name
        staged = self.settings.data_dir / "staging" / (call.id + ".png")
        try:
            index, total = await asyncio.to_thread(save_frame, path, args.frame_index, staged)
            name = Path(args.name).name
            if not name.lower().endswith(".png"):
                name += ".png"
            aid = await self.storage.ingest(
                staged,
                name,
                run.project_id,
                origin="agent_frame",
                role="candidate",
                description=f"从 {source_name} 第 {index} 帧抽取",
                source_info={
                    "source_asset_id": asset.id,
                    "source_blob_id": blob.sha256,
                    "source_name": source_name,
                    "frame_index": index,
                    "frames": total,
                },
                stable_asset_id=str(uuid5(NAMESPACE_URL, "aiv-agent-frame:" + call.id)),
            )
        except CompositionError as exc:
            raise AppError(exc.code, exc.message, 409, exc.details) from exc
        finally:
            staged.unlink(missing_ok=True)
        async with self.db.transaction() as session:
            stored = await session.get(CreativeAgentCall, call.id)
            stored.asset_id = aid
            current = await session.get(CreativeAgentRun, run.id)
            current.intent = "creation"
            image = await session.get(Asset, aid)
            media = image.media_info
        return success(
            {
                "asset_id": aid,
                "blob_id": image.blob_id,
                "frame_index": index,
                "frames": total,
                "width": media.get("width"),
                "height": media.get("height"),
                "media_type": "image",
            },
            resources=[{"kind": "frame", "asset_id": aid, "blob_id": image.blob_id}],
            effects=[{"type": "asset", "id": aid}],
        )

    @staticmethod
    def validate_plan_timing(targets, target_seconds):
        """Content-first timing: per-shot declarations must add up and stay in bounds."""
        for target in targets:
            duration = target.get("duration_seconds")
            if duration is not None and target.get("media_type") != "video":
                raise AppError(
                    "plan_duration",
                    "只有视频目标可以声明时长。",
                    409,
                    {"target_id": target["id"], "media_type": target.get("media_type")},
                )
        shots = [
            target
            for target in targets
            if target.get("media_type") == "video" and target.get("duration_seconds") is not None
        ]
        if not shots:
            if target_seconds is not None:
                raise AppError(
                    "plan_target",
                    "声明了成片总时长，就必须同时声明每个分镜的时长。",
                    409,
                    {"target_seconds": target_seconds},
                )
            return
        film = target_seconds is not None or len(shots) >= 3
        if film and not 3 <= len(shots) <= 8:
            raise AppError(
                "plan_shots",
                "成片镜头数必须在 3–8 个之间。",
                409,
                {"shots": len(shots)},
            )
        total = sum(float(target["duration_seconds"]) for target in shots)
        if total > 30 + 1e-9:
            raise AppError(
                "plan_total",
                "各镜时长之和超过 30 秒。",
                409,
                {"total_seconds": round(total, 2), "over_seconds": round(total - 30, 2)},
            )
        if target_seconds is not None:
            difference = total - float(target_seconds)
            if abs(difference) > 0.8 + 1e-9:
                raise AppError(
                    "plan_target",
                    "各镜时长之和与用户指定总时长的偏差超过 0.8 秒。",
                    409,
                    {
                        "total_seconds": round(total, 2),
                        "target_seconds": target_seconds,
                        "difference": round(difference, 2),
                    },
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
            executed = bool(run.execution_count or wrote_document)
            if executed:
                submitted = [requirement.model_dump() for requirement in args.requirements]
                changes = requirement_changes(run.work_plan.get("requirements", []), submitted)
                output_type_changed = run.work_plan.get("output_type") != args.output_type
                target_changed = (
                    "target_seconds" in args.model_fields_set
                    and run.work_plan.get("target_seconds") != args.target_seconds
                )
                stored_targets = {
                    item["id"]: item for item in run.work_plan.get("creation_targets", []) if "id" in item
                }
                duration_changed = any(
                    stored_targets.get(item.id, {}).get("duration_seconds") != item.duration_seconds
                    for item in args.creation_targets
                    if item.id in stored_targets
                )
                if changes or output_type_changed or target_changed or duration_changed:
                    raise AppError(
                        "goal_frozen",
                        "已执行创作后不能更换目标要求、标识、输出类型、分镜时长或总时长。",
                        409,
                        {
                            "changed_requirements": changes,
                            "output_type_changed": output_type_changed,
                            "target_seconds_changed": target_changed,
                            "duration_changed": duration_changed,
                            "hint": "只调整 items 的状态和说明；requirements 需逐字复用（标点和空白差异已容忍）。",
                        },
                    )
            old_operations = production_operations(run)
            plan = args.model_dump()
            if "target_seconds" not in args.model_fields_set:
                plan["target_seconds"] = run.work_plan.get("target_seconds")
            if executed:
                # Keep the originally frozen wording instead of storing punctuation variants.
                plan["requirements"] = run.work_plan.get("requirements", [])
            locked = run.work_plan.get("operations_frozen", False) or bool(
                await session.scalar(select(Task.id).where(Task.agent_run_id == run.id).limit(1))
            )
            if "creation_targets" in args.model_fields_set:
                old_titles = {item["id"]: item.get("asset_title") for item in old_operations}
                for spec, target in zip(args.creation_targets, plan["creation_targets"], strict=True):
                    title = target.pop("asset_title")
                    if locked or "asset_title" not in spec.model_fields_set:
                        title = old_titles.get(target["id"])
                    if title and target["media_type"] == "image":
                        target["asset_title"] = title
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
            if locked:
                # Compatibility for v1.0 plans, which had no separate targets.
                # Clearing or changing frozen targets stays an explicit error.
                if not proposed and any(item.get("media_type") is None for item in old_operations):
                    proposed = old_operations
                old_by_id = {item["id"]: item for item in old_operations}
                if set(old_by_id) != {item["id"] for item in proposed} or any(
                    old_by_id[item["id"]].get("media_type") not in (None, item.get("media_type"))
                    or set(old_by_id[item["id"]]["requirement_ids"]) != set(item["requirement_ids"])
                    or old_by_id[item["id"]]["text"] != item["text"]
                    or old_by_id[item["id"]].get("duration_seconds") != item.get("duration_seconds")
                    for item in proposed if item["id"] in old_by_id
                ):
                    raise AppError(
                        "creation_targets_frozen",
                        "生产已经开始，不能新增、改名、重绑生产目标或改动分镜时长以取得新执行身份。可自由调整计划步骤、说明、工具和参数；重做沿用原目标并关联返工证据。",
                        409,
                        {"existing_creation_targets": old_operations},
                    )
                if plan.get("target_seconds") != run.work_plan.get("target_seconds"):
                    raise AppError(
                        "goal_frozen",
                        "生产已经开始，不能更换成片总时长。",
                        409,
                        {
                            "stored_target_seconds": run.work_plan.get("target_seconds"),
                            "submitted_target_seconds": plan.get("target_seconds"),
                        },
                    )
            self.validate_plan_timing(proposed, plan.get("target_seconds"))
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
                    raise AppError(
                        "delivery_unchecked",
                        "交付清单中仍有未检查的成果，请逐份核对。",
                        409,
                        {"unchecked_asset_ids": sorted(set(assets) - {check.asset_id for check in args.checks})},
                    )
                for check in args.checks:
                    if check.asset_id not in assets:
                        raise AppError(
                            "delivery_evidence",
                            "检查必须对应交付清单中的实际成果。",
                            409,
                            {
                                "missing_asset_ids": sorted(
                                    {item.asset_id for item in args.checks} - set(assets)
                                ),
                                "delivered_asset_ids": list(assets),
                            },
                        )
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
                plan = dict(run.work_plan)
                items = [dict(item) for item in plan.get("items", [])]
                if any(item.get("state") != "completed" for item in items):
                    # A delivered Run must not leave its visible plan showing
                    # running/pending steps. Preserve the steps, close their state.
                    for item in items:
                        item["state"] = "completed"
                    plan["items"] = items
                    plan["revision"] = plan.get("revision", 0) + 1
                    plan["reconciled_at"] = now()
                    plan["reconciled"] = True
                    run.work_plan = plan
            run.completion = args.model_dump()
            run.state = "needs_attention" if args.kind == "needs_attention" else "completed"
            run.finished_at = now() if run.state == "completed" else None
            run.stop_reason = (
                {"code": "agent_report", "message": args.message} if run.state == "needs_attention" else None
            )
            if args.kind == "delivered":
                recycled = await recycle_unselected_assets(session, run.project_id, run_id=run.id)
                record_event(
                    session, "library.updated", run.project_id, run.id,
                    action="creative.delivered", recycled_asset_ids=recycled,
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
