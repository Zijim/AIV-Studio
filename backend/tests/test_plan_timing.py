"""Content-first timing rules for the plan and the natural-length composition option."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from app.config import ROOT, Settings
from app.creative_agent_schema import WorkPlan
from app.creative_agent_tools import AgentTools
from app.db import Database
from app.errors import AppError
from app.models import Base, Conversation, CreativeAgentRun, Project
from app.planner import Planner
from app.plans import PlanService
from app.registry import Registry
from app.storage import Storage
from app.worker import Worker


def target(identifier, duration=None):
    item = {
        "id": identifier,
        "text": identifier,
        "media_type": "video",
        "requirement_ids": ["r1"],
    }
    if duration is not None:
        item["duration_seconds"] = duration
    return item


class TimingRuleTests(unittest.TestCase):
    def validate(self, targets, target_seconds=None):
        AgentTools.validate_plan_timing(targets, target_seconds)

    def test_unequal_shots_with_exact_user_total_pass(self):
        self.validate(
            [target("s1", 4.0), target("s2", 6.5), target("s3", 4.0), target("s4", 5.5)],
            target_seconds=20.0,
        )

    def test_total_outside_tolerance_is_rejected(self):
        with self.assertRaises(AppError) as raised:
            self.validate(
                [target("s1", 4.0), target("s2", 6.5), target("s3", 5.5), target("s4", 6.0)],
                target_seconds=20.0,
            )
        self.assertEqual(raised.exception.code, "plan_target")
        self.assertEqual(raised.exception.details["difference"], 2.0)

    def test_sum_over_thirty_seconds_is_rejected(self):
        with self.assertRaises(AppError) as raised:
            self.validate([target("s1", 8.0), target("s2", 8.0), target("s3", 8.0), target("s4", 8.0)])
        self.assertEqual(raised.exception.code, "plan_total")
        self.assertEqual(raised.exception.details["over_seconds"], 2.0)

    def test_non_video_duration_is_rejected(self):
        item = {**target("ref"), "media_type": "image", "duration_seconds": 4.0}
        with self.assertRaises(AppError) as raised:
            self.validate([item])
        self.assertEqual(raised.exception.code, "plan_duration")

    def test_target_seconds_requires_declared_shots(self):
        with self.assertRaises(AppError) as raised:
            self.validate([target("s1")], target_seconds=20.0)
        self.assertEqual(raised.exception.code, "plan_target")

    def test_film_needs_three_shots(self):
        with self.assertRaises(AppError) as raised:
            self.validate([target("s1", 4.0), target("s2", 4.0)], target_seconds=8.0)
        self.assertEqual(raised.exception.code, "plan_shots")

    def test_more_than_eight_shots_is_rejected(self):
        with self.assertRaises(AppError) as raised:
            self.validate([target(f"s{i}", 4.0) for i in range(1, 10)])
        self.assertEqual(raised.exception.code, "plan_shots")

    def test_legacy_plan_without_durations_passes(self):
        self.validate([target("s1"), target("s2")])


class PlanStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for name in ("staging", "blobs", "previews", "registry", "executions", "backups", "logs"):
            (root / name).mkdir()
        self.settings = Settings(data_dir=root, minimum_free_bytes=0)
        self.db = Database(root / "test.sqlite3")
        self.addAsyncCleanup(self.db.close)
        async with self.db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text(
                    "CREATE VIRTUAL TABLE asset_search USING fts5(entry_id UNINDEXED, project_id UNINDEXED, asset_id UNINDEXED, body)"
                )
            )
        registry = Registry(ROOT / "tool_registry", root)
        for tool in registry.versions.values():
            tool["availability"] = "available"
        storage = Storage(self.db, self.settings)
        plans = PlanService(self.db, registry, storage, self.settings)
        planner = Planner(self.db, self.settings, registry, plans, storage)
        self.tools = AgentTools(
            SimpleNamespace(
                db=self.db,
                settings=self.settings,
                registry=registry,
                storage=storage,
                plans=plans,
                planner=planner,
                worker=Worker(self.db, None, registry, storage, self.settings),
            )
        )
        async with self.db.transaction() as session:
            project = Project(name="timing")
            session.add(project)
            await session.flush()
            conversation = Conversation(project_id=project.id, mode="autonomous")
            session.add(conversation)
            await session.flush()
            run = CreativeAgentRun(
                project_id=project.id,
                conversation_id=conversation.id,
                goal="20 秒酒馆短剧",
                goal_snapshot={"original": "20 秒酒馆短剧", "project": {}},
                scope={"selected": [], "use_key_assets": True, "image_roles": {}, "reference_policy": {"state": "resolved"}},
                access={"public_access": False, "can_manage": True},
                limits={"executions": 60, "composes": 6, "model_requests": 200, "tool_calls": 400, "no_progress": 3, "active_ms": 10_000_000},
                state="running",
                work_plan={},
            )
            session.add(run)
            await session.flush()
            self.run_id = run.id

    def plan(self, durations, target_seconds=None):
        payload = {
            "output_type": "video",
            "requirements": [{"id": "r1", "text": "20 秒成片"}],
            "items": [{"id": "edit", "text": "合成"}],
            "creation_targets": [
                {
                    "id": f"s{index}",
                    "text": f"镜头 {index}",
                    "media_type": "video",
                    "requirement_ids": ["r1"],
                    "duration_seconds": duration,
                }
                for index, duration in enumerate(durations, 1)
            ],
        }
        if target_seconds is not None:
            payload["target_seconds"] = target_seconds
        return WorkPlan(**payload)

    async def current_run(self):
        async with self.db.session() as session:
            return await session.get(CreativeAgentRun, self.run_id)

    async def test_plan_stores_durations_and_total(self):
        result = await self.tools.update_plan(
            await self.current_run(),
            SimpleNamespace(id="c1"),
            self.plan([4.0, 6.5, 4.0, 5.5], target_seconds=20.0),
            1,
        )
        self.assertEqual(result["data"]["target_seconds"], 20.0)
        self.assertEqual(
            [item["duration_seconds"] for item in result["data"]["creation_targets"]],
            [4.0, 6.5, 4.0, 5.5],
        )

    async def test_plan_rejects_total_mismatch(self):
        with self.assertRaises(AppError) as raised:
            await self.tools.update_plan(
                await self.current_run(),
                SimpleNamespace(id="c1"),
                self.plan([4.0, 6.5, 5.5, 6.0], target_seconds=20.0),
                1,
            )
        self.assertEqual(raised.exception.code, "plan_target")

    async def test_total_is_frozen_after_execution(self):
        await self.tools.update_plan(
            await self.current_run(),
            SimpleNamespace(id="c1"),
            self.plan([4.0, 6.5, 4.0, 5.5], target_seconds=20.0),
            1,
        )
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, self.run_id)
            run.execution_count = 1
        with self.assertRaises(AppError) as raised:
            await self.tools.update_plan(
                await self.current_run(),
                SimpleNamespace(id="c2"),
                self.plan([4.0, 6.5, 4.0, 5.5], target_seconds=21.0),
                1,
            )
        self.assertEqual(raised.exception.code, "goal_frozen")

    async def test_duration_is_frozen_after_execution(self):
        await self.tools.update_plan(
            await self.current_run(),
            SimpleNamespace(id="c1"),
            self.plan([4.0, 6.5, 4.0, 5.5], target_seconds=20.0),
            1,
        )
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, self.run_id)
            run.execution_count = 1
        with self.assertRaises(AppError) as raised:
            await self.tools.update_plan(
                await self.current_run(),
                SimpleNamespace(id="c2"),
                self.plan([4.0, 6.5, 5.0, 4.5], target_seconds=20.0),
                1,
            )
        self.assertEqual(raised.exception.code, "goal_frozen")

    async def test_omitted_total_keeps_the_stored_value(self):
        await self.tools.update_plan(
            await self.current_run(),
            SimpleNamespace(id="c1"),
            self.plan([4.0, 6.5, 4.0, 5.5], target_seconds=20.0),
            1,
        )
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, self.run_id)
            run.execution_count = 1
        result = await self.tools.update_plan(
            await self.current_run(),
            SimpleNamespace(id="c2"),
            self.plan([4.0, 6.5, 4.0, 5.5]),
            1,
        )
        self.assertEqual(result["data"]["target_seconds"], 20.0)


if __name__ == "__main__":
    unittest.main()
