import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import event

from app.db import Database
from app.models import Artifact, Asset, Base, Blob, Execution, Project, ProjectAsset, Task
from app.task_media import resolve_task_media


class TaskMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "previews").mkdir()
        self.db = Database(self.root / "app.sqlite3")
        async with self.db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.storage = SimpleNamespace(resolve=lambda name: self.root / name)
        self.registry = SimpleNamespace(get=lambda *_: {"inputs": [{"slot": "source_image", "label": "编辑原图"}]})
        async with self.db.transaction() as s:
            s.add(Project(id="project", name="素材关联验证"))
            await s.flush()
            s.add(
                Task(
                    id="task",
                    project_id="project",
                    plan_id="plan",
                    plan_revision=1,
                    origin="manual",
                    queue_seq=1,
                    candidate_count=1,
                )
            )
            await s.flush()
            await self.execution(s, "e1", 0)
            await self.asset(s, "input", None)
            await self.asset(s, "output", "e1")
            await self.asset(s, "preview", "e1", role="preview", removed=True)
            s.add(
                Artifact(
                    id="final",
                    execution_id="e1",
                    node_id="final",
                    field="images",
                    item_index=0,
                    role="final",
                    reference={"filename": "result_00001_.png"},
                    state="saved",
                    asset_id="output",
                )
            )
            s.add(
                Artifact(
                    id="preview-record",
                    execution_id="e1",
                    node_id="preview",
                    field="images",
                    item_index=0,
                    role="preview",
                    reference={"filename": "preview.png"},
                    state="saved",
                    asset_id="preview",
                    representation_of="output",
                )
            )
            e = await s.get(Execution, "e1")
            e.inputs = [{"slot": "source_image", "asset_id": "input", "blob_id": "blob-input"}]

    async def asyncTearDown(self):
        await self.db.close()
        self.directory.cleanup()

    async def execution(self, s, eid, index, configuration=None):
        e = Execution(
            id=eid,
            task_id="task",
            candidate_index=index,
            client_id="client",
            tool_id="tool",
            tool_version="1.0.0",
            seed=123 + index,
            parameters={},
            inputs=[],
            configuration=configuration or {},
        )
        s.add(e)
        await s.flush()
        return e

    async def asset(self, s, aid, execution_id, *, role="final", removed=False, blob_id=None):
        blob_id = blob_id or "blob-" + aid
        if not await s.get(Blob, blob_id):
            s.add(Blob(sha256=blob_id, byte_size=10, relative_path=blob_id, mime_type="image/png", extension="png"))
            (self.root / blob_id).write_bytes(b"asset")
            await s.flush()
        s.add(
            Asset(
                id=aid,
                name="result_00001_.png",
                blob_id=blob_id,
                media_type="image",
                origin="generated",
                execution_id=execution_id,
                role=role,
                preview_path=aid + ".png",
                media_info={"width": 1024, "height": 1024},
            )
        )
        (self.root / "previews" / (aid + ".png")).write_bytes(b"preview")
        await s.flush()
        s.add(ProjectAsset(project_id="project", asset_id=aid, removed_at="2026-01-01" if removed else None))
        await s.flush()

    async def resolve(self):
        from sqlalchemy import select

        async with self.db.session() as s:
            tasks = list(await s.scalars(select(Task)))
            executions = list(await s.scalars(select(Execution).order_by(Execution.candidate_index)))
            return await resolve_task_media(s, tasks, executions, self.storage, self.registry)

    async def test_same_filename_has_distinct_input_and_output_ids_and_no_duplicate_preview(self):
        task, executions, _ = await self.resolve()
        media = task["task"]
        self.assertEqual([a["asset_id"] for a in media["inputs"]], ["input"])
        self.assertEqual([a["asset_id"] for a in media["outputs"]], ["output"])
        self.assertEqual(media["other_outputs"], [])
        self.assertEqual(media["inputs"][0]["name"], media["outputs"][0]["name"])
        self.assertTrue(media["inputs"][0]["can_open"])
        self.assertEqual(executions["e1"]["inputs"][0]["label"], "编辑原图")

    async def test_multiple_candidates_sharing_blob_remain_distinct_outputs(self):
        async with self.db.transaction() as s:
            e = await self.execution(s, "e2", 1)
            e.inputs = [{"slot": "source_image", "asset_id": "input", "blob_id": "blob-input"}]
            await self.asset(s, "output-2", "e2", blob_id="blob-output")
        task, _, _ = await self.resolve()
        self.assertEqual(len(task["task"]["inputs"]), 1)
        self.assertEqual({a["asset_id"] for a in task["task"]["outputs"]}, {"output", "output-2"})

    async def test_two_steps_separate_intermediate_from_final_and_dependency_input(self):
        async with self.db.transaction() as s:
            first = await s.get(Execution, "e1")
            first.configuration = {"step_count": 2, "step_index": 0, "step_label": "准备起始画面"}
            second = await self.execution(
                s, "e2", 1, {"step_count": 2, "step_index": 1, "depends_on_execution_id": "e1"}
            )
            second.inputs = [{"slot": "source_image", "asset_id": "output", "blob_id": "blob-output"}]
            await self.asset(s, "video", "e2")
        task, executions, _ = await self.resolve()
        self.assertEqual([a["asset_id"] for a in task["task"]["inputs"]], ["input"])
        self.assertEqual([a["asset_id"] for a in task["task"]["intermediates"]], ["output"])
        self.assertTrue(task["task"]["intermediates"][0]["is_intermediate"])
        self.assertEqual([a["asset_id"] for a in task["task"]["outputs"]], ["video"])
        self.assertEqual(executions["e2"]["inputs"][0]["asset_id"], "output")

    async def test_removed_input_remains_readable_without_restoring_membership(self):
        async with self.db.transaction() as s:
            m = await s.get(ProjectAsset, ("project", "input"))
            m.removed_at = "2026-01-01"
        tasks, _, _ = await self.resolve()
        self.assertTrue(tasks["task"]["inputs"][0]["project_removed"])
        self.assertTrue(tasks["task"]["inputs"][0]["can_preview"])
        async with self.db.session() as s:
            self.assertEqual((await s.get(ProjectAsset, ("project", "input"))).removed_at, "2026-01-01")

    async def test_missing_original_keeps_available_cached_preview(self):
        (self.root / "blob-output").unlink()
        tasks, _, _ = await self.resolve()
        item = tasks["task"]["outputs"][0]
        self.assertFalse(item["can_open"])
        self.assertTrue(item["can_preview"])
        self.assertIsNone(item["content_url"])
        self.assertEqual(item["presence"], "missing")

    async def test_version_changed_input_does_not_preview_current_pixels(self):
        async with self.db.transaction() as s:
            e = await s.get(Execution, "e1")
            e.inputs = [{"slot": "source_image", "asset_id": "input", "blob_id": "previous-file"}]
        tasks, _, _ = await self.resolve()
        item = tasks["task"]["inputs"][0]
        self.assertEqual(item["presence"], "changed")
        self.assertFalse(item["can_preview"])

    async def test_unknown_input_and_unsaved_output_are_explicit_placeholders(self):
        async with self.db.transaction() as s:
            e = await s.get(Execution, "e1")
            e.inputs = [{"slot": "source_image", "asset_id": "gone"}]
            s.add(
                Artifact(
                    id="pending",
                    execution_id="e1",
                    node_id="other-final",
                    field="images",
                    item_index=0,
                    role="final",
                    reference={"filename": "pending.png"},
                    state="pending",
                )
            )
        tasks, _, _ = await self.resolve()
        item = tasks["task"]["inputs"][0]
        self.assertEqual(item["asset_id"], "gone")
        self.assertFalse(item["can_preview"])
        pending = next(a for a in tasks["task"]["outputs"] if a["presence"] == "unsaved")
        self.assertIsNone(pending["asset_id"])
        self.assertFalse(pending["can_preview"])

    async def test_batch_resolution_has_constant_query_count(self):
        async with self.db.transaction() as s:
            for i in range(1, 20):
                e = await self.execution(s, f"e{i + 1}", i)
                e.inputs = [{"slot": "source_image", "asset_id": "input", "blob_id": "blob-input"}]
        statements = []

        def record(*args):
            statements.append(args[2])

        event.listen(self.db.engine.sync_engine, "before_cursor_execute", record)
        try:
            await self.resolve()
        finally:
            event.remove(self.db.engine.sync_engine, "before_cursor_execute", record)
        # Task/execution reads + four batch media queries, independent of thumbnail count.
        self.assertLessEqual(len(statements), 6)


if __name__ == "__main__":
    unittest.main()
