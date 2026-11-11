"""Real DB boundary regressions; providers are fixed, no GPU/model service calls."""

import json
from types import SimpleNamespace

from PIL import Image
from sqlalchemy import select
from test_creative_agent import AgentTestCase, plan, response

from app.creative_agent import CreativeAgent
from app.creative_agent_policy import production_operations
from app.creative_agent_schema import Answer, Creation, ResourceScope, Search, WorkPlan
from app.creative_agent_scope import require_submission_inputs, scoped_asset
from app.errors import AppError
from app.models import CreativeAgentRun, Execution, ProjectAsset, Task, uid


class CreativeAgentBoundaryTests(AgentTestCase):
    async def seal(self, rid, **values):
        async with self.db.transaction() as session:
            (await session.get(CreativeAgentRun, rid)).state = "running"
        return await self.agent.tools.set_resource_scope(
            await self.run_record(rid), SimpleNamespace(id=uid()), ResourceScope(**values), 1
        )

    async def finish_image_task(self, task, execution):
        path = self.settings.data_dir / "staging" / f"{uid()}.png"
        Image.new("RGB", (512, 512), "red").save(path)
        aid = await self.state.storage.ingest(
            path,
            "observed-red.png",
            self.pid,
            origin="generated",
            role="final",
            execution_id=execution.id,
            candidate_index=0,
        )
        async with self.db.transaction() as session:
            e = await session.get(Execution, execution.id)
            e.execution_state, e.storage_state = "succeeded", "complete"
            (await session.get(Task, task.id)).state = "completed"
        return aid

    async def test_natural_language_scope_is_decided_before_any_material_metadata(self):
        selected = await self.import_text("chosen.txt", "SELECTED_CONTENT")
        key = await self.import_text("KEY_NAME_MUST_NOT_LEAK.txt", "KEY_CONTENT_MUST_NOT_LEAK")
        async with self.db.transaction() as session:
            member = await session.get(ProjectAsset, (self.pid, key))
            member.curated_at, member.note = "2026-11-10", "KEY_NOTE_MUST_NOT_LEAK"
        goal = "只使用我选中的 chosen.txt，不要读取项目关键素材。告诉我当前开放了哪些素材。"
        rid = await self.start(goal, selected_asset_ids=[selected], resolve_scope=False)
        requests = []

        async def provider(request):
            requests.append(request)
            serialized = json.dumps(request, ensure_ascii=False)
            self.assertNotIn("KEY_NAME_MUST_NOT_LEAK", serialized)
            self.assertNotIn("KEY_NOTE_MUST_NOT_LEAK", serialized)
            self.assertNotIn("KEY_CONTENT_MUST_NOT_LEAK", serialized)
            if len(requests) == 1:
                self.assertNotIn("SELECTED_CONTENT", serialized)
                return response(("scope", "set_resource_scope", {"mode": "selected_only", "user_quote": goal}))
            if len(requests) == 2:
                return response(("search", "search_resources", {}))
            if len(requests) == 3:
                return response(("try_key", "read_resource", {"asset_id": key}))
            return response(("reply", "finish_run", {"kind": "reply", "message": "只开放明确选中的素材。"}))

        self.agent.provider = provider
        await self.agent.drive(rid)
        calls = await self.calls(rid)
        search = next(call.result["data"] for call in calls if call.call_id == "search")
        self.assertEqual(search["total"], 1)
        self.assertEqual(search["items"][0]["asset_id"], selected)
        self.assertEqual(next(c for c in calls if c.call_id == "try_key").result["error"]["code"], "resource_scope")

    async def test_unsealed_scope_blocks_direct_read_preview_generation_and_worker_submission(self):
        key = await self.key_image()
        rid = await self.start(selected_asset_ids=[key], resolve_scope=False)
        run = await self.run_record(rid)
        context = (await self.agent.tools.get_project_context(run, None, None, 1))["data"]
        self.assertEqual(context["materials"]["items"], [])
        self.assertEqual(context["materials"]["total"], 0)
        with self.assertRaisesRegex(AppError, "set_resource_scope"):
            await self.agent.tools.search_resources(run, None, Search(), 1)
        async with self.db.session() as session:
            with self.assertRaises(AppError) as blocked:
                await scoped_asset(session, run, key)
            self.assertEqual(blocked.exception.code, "resource_scope_pending")
        args = Creation(
            capability_id="image.generate.zimage",
            operation_id="main",
            purpose="test",
            prompt_document={"positive_prompt": "blue boat"},
        )
        with self.assertRaises(AppError) as blocked:
            await self.agent.tools.prepare_creation(run, args)
        self.assertEqual(blocked.exception.code, "resource_scope_pending")
        # Even a forged queued task with zero inputs must not bypass sealing.
        async with self.db.session() as session:
            for inputs in ([], [{"asset_id": key}]):
                with self.assertRaises(AppError):
                    await require_submission_inputs(
                        session,
                        SimpleNamespace(agent_run_id=rid, project_id=self.pid, conversation_id=self.cid),
                        SimpleNamespace(inputs=inputs),
                    )

    async def test_exact_named_scope_exclusions_and_original_ceiling_are_enforced(self):
        chosen = await self.import_text("chosen.txt", "chosen content")
        denied = await self.import_text("denied.txt", "denied content")
        hidden = await self.import_text("not-authorized.txt", "not authorized content")
        async with self.db.transaction() as session:
            (await session.get(ProjectAsset, (self.pid, chosen))).curated_at = "2026-11-10"
            (await session.get(ProjectAsset, (self.pid, denied))).curated_at = "2026-11-10"
        goal = "只用 chosen.txt 和 denied.txt，但是排除 denied.txt。不要使用 not-authorized.txt。"
        rid = await self.start(goal, resolve_scope=False)
        with self.assertRaises(AppError):
            await self.seal(rid, mode="only", only_names=["not-authorized.txt"], user_quote=goal)
        with self.assertRaises(AppError):
            await self.seal(rid, mode="only", only_names=["chosen.txt"], user_quote="模型自己编造的授权")
        await self.seal(
            rid, mode="only", only_names=["chosen.txt", "denied.txt"], exclude_names=["denied.txt"], user_quote=goal
        )
        run = await self.run_record(rid)
        async with self.db.session() as session:
            resources = await self.agent.tools.resources(session, run, Search(limit=1, offset=0))
            self.assertEqual(resources["total"], 1)
            self.assertEqual(resources["items"][0]["asset_id"], chosen)
            for aid in (denied, hidden):
                with self.assertRaises(AppError):
                    await scoped_asset(session, run, aid)
        with self.assertRaises(AppError) as widened:
            await self.seal(rid, mode="available")
        self.assertEqual(widened.exception.code, "scope_frozen")
        async with self.db.transaction() as session:
            (await session.get(ProjectAsset, (self.pid, chosen))).curated_at = None
        async with self.db.session() as session:
            with self.assertRaises(AppError):
                await scoped_asset(session, await session.get(CreativeAgentRun, rid), chosen)

    async def test_key_switch_cannot_be_reopened_by_scope_selection(self):
        key = await self.key_image()
        rid = await self.start("只用 key.png", use_key_assets=False, resolve_scope=False)
        with self.assertRaises(AppError):
            await self.seal(rid, mode="only", only_names=["key.png"], user_quote="只用 key.png")
        await self.seal(rid, mode="available")
        async with self.db.session() as session:
            self.assertEqual((await self.agent.tools.resources(session, await self.run_record(rid)))["total"], 0)
            with self.assertRaises(AppError):
                await scoped_asset(session, await self.run_record(rid), key)

    async def test_no_reference_creation_still_uses_own_document_and_scope_receipt_is_idempotent(self):
        selected = await self.import_text("previous.txt")
        key = await self.key_image()
        rid = await self.start("无参考写一段说明", selected_asset_ids=[selected], resolve_scope=False)
        async with self.db.transaction() as session:
            (await session.get(CreativeAgentRun, rid)).state = "running"
        call = SimpleNamespace(id=uid())
        args = ResourceScope(mode="none", user_quote="无参考写一段说明")
        first = await self.agent.tools.set_resource_scope(await self.run_record(rid), call, args, 1)
        second = await self.agent.tools.set_resource_scope(await self.run_record(rid), call, args, 1)
        self.assertEqual(first["data"], second["data"])
        with self.assertRaises(AppError) as conflict:
            await self.agent.tools.set_resource_scope(
                await self.run_record(rid), call, ResourceScope(mode="available"), 1
            )
        self.assertEqual(conflict.exception.code, "scope_idempotency_conflict")
        async with self.db.transaction() as session:
            (await session.get(CreativeAgentRun, rid)).state = "queued"
        count = 0

        async def provider(request):
            nonlocal count
            count += 1
            if count == 1:
                return response(("p", "update_plan", plan("text")))
            if count == 2:
                return response(("doc", "write_document", {"name": "新文档", "content": "新内容"}))
            aid = next(c.result["data"]["asset_id"] for c in await self.calls(rid) if c.call_id == "doc")
            if count == 3:
                return response(("read", "read_resource", {"asset_id": aid}))
            return response(
                (
                    "finish",
                    "finish_run",
                    {
                        "kind": "delivered",
                        "message": "完成",
                        "asset_ids": [aid],
                        "checks": [
                            {
                                "requirement_id": "subject",
                                "asset_id": aid,
                                "evidence_call_id": "read",
                                "outcome": "passed",
                                "observation": "正文已读",
                                "coverage": "全文",
                            }
                        ],
                    },
                )
            )

        self.agent.provider = provider
        await self.agent.drive(rid)
        self.assertEqual((await self.run_record(rid)).state, "completed")
        async with self.db.session() as session:
            run = await self.run_record(rid)
            resources = await self.agent.tools.resources(session, run)
            self.assertEqual(resources["total"], 1)
            self.assertNotIn(selected, json.dumps(resources))
            self.assertNotIn(key, json.dumps(resources))

    async def test_renaming_plan_steps_cannot_mint_new_production_or_escape_issue_limit(self):
        self.settings.agent_max_issue_reworks = 0
        self.settings.agent_max_no_progress = 10
        rid, task, execution, args = await self.queued_creation()
        aid = await self.finish_image_task(task, execution)
        count = 0

        async def provider(request):
            nonlocal count
            count += 1
            if count == 1:
                return response(("see", "inspect_media", {"asset_id": aid, "purpose": "核对明确颜色偏差"}))
            renamed = plan()
            renamed["items"] = [{"id": "renamed_step", "text": "修正同一主体颜色"}]
            if count == 2:
                return response(("rename_display", "update_plan", renamed))
            if count == 3:
                return response(("renamed_generation", "invoke_creation", {**args, "operation_id": "renamed_step"}))
            if count == 4:
                changed = {
                    **renamed,
                    "creation_targets": [
                        {
                            **renamed["creation_targets"][0],
                            "id": "renamed_target",
                        }
                    ],
                }
                return response(("rename_target", "update_plan", changed))
            if count == 5:
                return response(("omit_rework", "invoke_creation", args))
            if count == 6:
                return response(
                    (
                        "over_limit",
                        "invoke_creation",
                        {
                            **args,
                            "rework": [
                                {
                                    "requirement_id": "subject",
                                    "source_asset_id": aid,
                                    "evidence_call_id": "see",
                                    "problem": "实际主体颜色错误",
                                    "reason": "真实观察明确偏差",
                                }
                            ],
                        },
                    )
                )
            return response(("stop", "finish_run", {"kind": "needs_attention", "message": "没有可用返工名额"}))

        self.agent.provider = provider
        await self.agent.drive(rid)
        calls = {call.call_id: call for call in await self.calls(rid)}
        self.assertTrue(calls["rename_display"].result["ok"])
        self.assertEqual(calls["renamed_generation"].result["error"]["code"], "operation_identity")
        self.assertEqual(calls["rename_target"].result["error"]["code"], "creation_targets_frozen")
        self.assertEqual(calls["omit_rework"].result["error"]["code"], "rework_evidence_required")
        self.assertEqual(calls["over_limit"].result["error"]["code"], "rework_limit")
        self.assertEqual((await self.run_record(rid)).execution_count, 1)
        async with self.db.session() as session:
            self.assertEqual(len((await session.scalars(select(Task))).all()), 1)

    async def test_generation_identity_is_frozen_even_if_display_plan_is_replaced_after_restart(self):
        rid, task, execution, args = await self.queued_creation()
        run = await self.run_record(rid)
        original = production_operations(run)
        recovered = CreativeAgent(self.state)
        self.addAsyncCleanup(recovered.stop)
        await recovered.recover()
        async with self.db.transaction() as session:
            (await session.get(CreativeAgentRun, rid)).state = "running"
        replacement = plan()
        replacement["items"] = [{"id": "new_steps", "text": "调整可见步骤"}]
        changed = await recovered.tools.update_plan(run, None, WorkPlan(**replacement), 1)
        self.assertEqual(changed["data"]["production_operations"], original)
        replacement["creation_targets"][0]["text"] = "更换生产目标含义来重做"
        with self.assertRaises(AppError) as rebound:
            await recovered.tools.update_plan(await self.run_record(rid), None, WorkPlan(**replacement), 1)
        self.assertEqual(rebound.exception.code, "creation_targets_frozen")
        replacement["creation_targets"][0]["text"] = original[0]["text"]
        replacement["creation_targets"].append(
            {
                "id": "new_free_attempt",
                "text": "再次尝试",
                "media_type": "image",
                "requirement_ids": ["subject"],
            }
        )
        with self.assertRaises(AppError) as rejected:
            await recovered.tools.update_plan(await self.run_record(rid), None, WorkPlan(**replacement), 1)
        self.assertEqual(rejected.exception.code, "creation_targets_frozen")
        replacement["creation_targets"] = []
        with self.assertRaises(AppError) as cleared:
            await recovered.tools.update_plan(await self.run_record(rid), None, WorkPlan(**replacement), 1)
        self.assertEqual(cleared.exception.code, "creation_targets_frozen")

    async def test_declared_image_to_video_targets_remain_independent_of_editable_steps(self):
        count = 0

        async def provider(request):
            nonlocal count
            count += 1
            if count == 1:
                p = plan("video")
                p["creation_targets"] = [
                    {"id": "opening", "text": "首帧", "media_type": "image", "requirement_ids": ["subject"]},
                    {"id": "movie", "text": "最终视频", "media_type": "video", "requirement_ids": ["subject"]},
                ]
                p["items"] = [{"id": "design", "text": "自行安排首帧和视频步骤"}]
                return response(("p", "update_plan", p))
            if count == 2:
                return response(
                    (
                        "image",
                        "invoke_creation",
                        {
                            "capability_id": "image.generate.zimage",
                            "operation_id": "opening",
                            "purpose": "首帧",
                            "prompt_document": {"positive_prompt": "blue boat"},
                        },
                    )
                )
            calls = await self.calls(rid)
            image_task = next(call.result["data"]["task_id"] for call in calls if call.call_id == "image")
            if count == 3:
                return response(("wait_image", "wait_for_tasks", {"task_ids": [image_task]}))
            if count == 4:
                p = plan("video")
                del p["creation_targets"]
                p["items"] = [
                    {"id": "frame_saved", "text": "首帧已保存", "state": "completed"},
                    {"id": "animate_frame", "text": "使用首帧生成视频", "state": "running"},
                ]
                return response(("status", "update_plan", p))
            if count == 5:
                return response(
                    (
                        "video",
                        "invoke_creation",
                        {
                            "capability_id": "video.generate.minimax_i2v",
                            "operation_id": "movie",
                            "purpose": "连续镜头",
                            "inputs": [{"slot": "source_image", "asset_id": aid}],
                            "parameters": {"aspect_ratio": "source", "megapixels": 0.6, "duration_seconds": 5},
                            "prompt_document": {
                                "shots": [{"index": 1, "description": "A blue boat moves on water."}],
                                "overall_soundscape": "N/A",
                                "non_diegetic_music": "N/A",
                                "style_description": "Cool, misty natural light.",
                            },
                        },
                    )
                )
            video_task = next(call.result["data"]["task_id"] for call in calls if call.call_id == "video")
            return response(("wait_video", "wait_for_tasks", {"task_ids": [video_task]}))

        self.agent.provider = provider
        rid = await self.start("生成一个蓝色纸船视频")
        await self.agent.drive(rid)
        async with self.db.session() as session:
            task = await session.scalar(select(Task).where(Task.agent_run_id == rid))
            execution = await session.scalar(select(Execution).where(Execution.task_id == task.id))
        aid = await self.finish_image_task(task, execution)
        await self.agent.drive(rid)
        run = await self.run_record(rid)
        self.assertEqual(run.execution_count, 2, run.stop_reason)
        self.assertEqual(run.issues, {})
        calls = {call.call_id: call for call in await self.calls(rid)}
        self.assertTrue(calls["status"].result["ok"], calls["status"].result)
        self.assertEqual(calls["status"].result["data"]["creation_targets"], calls["p"].result["data"]["creation_targets"])
        self.assertEqual(production_operations(run), calls["p"].result["data"]["production_operations"])
        self.assertTrue(calls["video"].result["ok"], calls["video"].result)
        video_params = calls["video"].result["data"]["parameters"]
        self.assertEqual((video_params["width"], video_params["height"]), (768, 768))
        self.assertIn("Cool, misty natural light.", video_params["prompt"])
        self.assertIn("A blue boat moves on water.", video_params["prompt"])
        self.assertNotEqual(
            calls["image"].result["data"]["metadata"]["operation_key"],
            calls["video"].result["data"]["metadata"]["operation_key"],
        )

    async def test_necessary_answer_grants_only_new_explicit_material_without_reopening_keys(self):
        old_selected = await self.import_text("old.txt", "old contents")
        key = await self.key_image()
        granted = await self.import_text("answer.txt", "new explicit answer")
        rid = await self.start("先问我需要哪份素材", selected_asset_ids=[old_selected], resolve_scope=False)
        await self.seal(rid, mode="none")
        async with self.db.transaction() as session:
            (await session.get(CreativeAgentRun, rid)).state = "queued"

        async def provider(request):
            return response(
                (
                    "ask",
                    "ask_user",
                    {
                        "question": "请指定所需素材",
                        "reason": "缺少必要依据",
                        "allow_assets": True,
                    },
                )
            )

        self.agent.provider = provider
        await self.agent.drive(rid)
        await self.agent.answer(
            rid,
            Answer(
                control_version=1,
                call_id="ask",
                answer="用这份素材",
                selected_asset_ids=[granted],
                idempotency_key=uid(),
            ),
        )
        run = await self.run_record(rid)
        async with self.db.session() as session:
            resources = await self.agent.tools.resources(session, run)
            self.assertEqual({item["asset_id"] for item in resources["items"]}, {granted})
            for aid in (old_selected, key):
                with self.assertRaises(AppError):
                    await scoped_asset(session, run, aid)

    async def test_legacy_run_keeps_original_operation_identity_on_repeated_plan_updates(self):
        rid, task, execution, args = await self.queued_creation()
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, rid)
            run.scope = {key: value for key, value in run.scope.items() if key != "reference_policy"}
            run.work_plan = {
                key: value
                for key, value in run.work_plan.items()
                if key not in ("creation_targets", "production_operations", "operations_frozen")
            }
            run.state = "running"
        before = production_operations(await self.run_record(rid))
        for index in range(2):
            updated = plan()
            updated["creation_targets"] = []
            updated["items"] = [{"id": f"display{index}", "text": "调整工作步骤"}]
            result = await self.agent.tools.update_plan(await self.run_record(rid), None, WorkPlan(**updated), 1)
            self.assertEqual(result["data"]["production_operations"], before)
        updated["creation_targets"] = [
            {
                "id": "new_attempt",
                "text": "重新命名",
                "media_type": "image",
                "requirement_ids": ["subject"],
            }
        ]
        with self.assertRaises(AppError) as rejected:
            await self.agent.tools.update_plan(await self.run_record(rid), None, WorkPlan(**updated), 1)
        self.assertEqual(rejected.exception.code, "creation_targets_frozen")

    async def test_rework_cannot_borrow_another_production_targets_observation(self):
        count = 0

        async def provider(request):
            nonlocal count
            count += 1
            if count == 1:
                p = plan()
                p["creation_targets"].append(
                    {
                        "id": "other",
                        "text": "另一份独立产物",
                        "media_type": "image",
                        "requirement_ids": ["subject"],
                    }
                )
                return response(("plan", "update_plan", p))
            if count in (2, 3):
                return response(
                    (
                        f"create{count}",
                        "invoke_creation",
                        {
                            "capability_id": "image.generate.zimage",
                            "operation_id": "main" if count == 2 else "other",
                            "purpose": "独立产物",
                            "prompt_document": {"positive_prompt": "blue boat"},
                        },
                    )
                )
            calls = await self.calls(rid)
            if count == 4:
                ids = [c.result["data"]["task_id"] for c in calls if c.name == "invoke_creation"]
                return response(("wait", "wait_for_tasks", {"task_ids": ids}))
            if count == 5:
                return response(("observe_main", "inspect_media", {"asset_id": source, "purpose": "主体颜色错误"}))
            if count == 6:
                return response(
                    (
                        "borrow_evidence",
                        "invoke_creation",
                        {
                            "capability_id": "image.generate.qwen",
                            "operation_id": "other",
                            "purpose": "修正另一份产物",
                            "prompt_document": {"positive_prompt": "blue boat"},
                            "rework": [
                                {
                                    "requirement_id": "subject",
                                    "source_asset_id": source,
                                    "evidence_call_id": "observe_main",
                                    "problem": "实际颜色错误",
                                    "reason": "试图挪用第一份产物的证据",
                                }
                            ],
                        },
                    )
                )
            return response(("stop", "finish_run", {"kind": "needs_attention", "message": "保留原成果"}))

        self.agent.provider = provider
        rid = await self.start("生成两份蓝色纸船素材")
        await self.agent.drive(rid)
        async with self.db.session() as session:
            tasks = (await session.scalars(select(Task).order_by(Task.queue_seq))).all()
            executions = [await session.scalar(select(Execution).where(Execution.task_id == task.id)) for task in tasks]
        source = await self.finish_image_task(tasks[0], executions[0])
        await self.finish_image_task(tasks[1], executions[1])
        await self.agent.drive(rid)
        result = next(c.result for c in await self.calls(rid) if c.call_id == "borrow_evidence")
        self.assertEqual(result["error"]["code"], "rework_identity")
        run = await self.run_record(rid)
        self.assertEqual(run.execution_count, 2)
        self.assertEqual(run.issues, {})
