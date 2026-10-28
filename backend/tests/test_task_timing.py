import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.api import task_detail, tasks
from app.db import Database
from app.models import Base, Execution, Project, Task
from app.task_timing import event_time, execution_timing, history_times
from app.worker import Worker


def history(start=1000000, finish=1060500, event="execution_success"):
    return {
        "status": {
            "status_str": "success" if event == "execution_success" else "error",
            "messages": [["execution_start", {"timestamp": start}], [event, {"timestamp": finish}]],
        },
        "prompt": [None, None, {}],
        "outputs": {},
    }


def execution(**values):
    return SimpleNamespace(
        **{
            "history": None,
            "started_at": None,
            "finished_at": None,
            "execution_state": "pending",
            "submitted_payload": None,
            **values,
        }
    )


class TimingTests(unittest.TestCase):
    def test_history_overrides_late_collection_and_observed_start(self):
        timing = execution_timing(
            execution(
                history=history(),
                started_at=event_time(1005000),
                finished_at=event_time(9900000),
                execution_state="succeeded",
            )
        )
        self.assertEqual(timing["elapsed_seconds"], 60.5)
        self.assertEqual(timing["started_at"], event_time(1000000))
        self.assertEqual(timing["finished_at"], event_time(1060500))
        self.assertEqual(timing["source"], "history")

    def test_failed_and_cancelled_histories_have_actual_end(self):
        for event in ("execution_error", "execution_interrupted"):
            timing = execution_timing(execution(history=history(event=event), execution_state="cancelled"))
            self.assertEqual(timing["elapsed_seconds"], 60.5)
            self.assertFalse(timing["is_running"])

    def test_unstarted_queue_and_pre_submission_cancellation_are_zero(self):
        for state in ("pending", "waiting_external", "blocked", "cancelled", "failed"):
            timing = execution_timing(execution(execution_state=state))
            self.assertEqual(timing["elapsed_seconds"], 0)
            self.assertIsNone(timing["started_at"])
            self.assertFalse(timing["is_running"])
            self.assertFalse(timing["incomplete"])

    def test_old_success_and_lost_evidence_are_incomplete(self):
        for state in ("succeeded", "unknown", "interrupted"):
            timing = execution_timing(execution(execution_state=state, submitted_payload={"prompt": {}}))
            self.assertIsNone(timing["elapsed_seconds"])
            self.assertTrue(timing["incomplete"])

    def test_live_duration_starts_at_execution_not_task_creation(self):
        start = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
        timing = execution_timing(execution(execution_state="running", started_at=start))
        self.assertTrue(timing["is_running"])
        self.assertAlmostEqual(timing["elapsed_seconds"], 15, delta=1)

    def test_lost_evidence_does_not_treat_reconciliation_time_as_end(self):
        timing = execution_timing(
            execution(
                execution_state="unknown",
                started_at=event_time(1000000),
                finished_at=event_time(9900000),
            )
        )
        self.assertIsNone(timing["finished_at"])
        self.assertIsNone(timing["elapsed_seconds"])
        self.assertFalse(timing["is_running"])
        self.assertTrue(timing["incomplete"])

    def test_invalid_timestamps_and_reversed_history(self):
        for value in (None, "123", True, -1, float("inf"), float("nan"), 1e30):
            self.assertIsNone(event_time(value))
        self.assertEqual(history_times(history(finish=900000)), (event_time(1000000), None))
        self.assertEqual(history_times({"status": {"messages": [[], ["execution_start", None]]}}), (None, None))


class TimingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.db = Database(self.path / "app.sqlite3")
        async with self.db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.db.transaction() as session:
            session.add(Project(id="project", name="计时验证"))
            await session.flush()
            session.add(
                Task(
                    id="task",
                    project_id="project",
                    plan_id="plan",
                    plan_revision=1,
                    origin="manual",
                    queue_seq=1,
                    candidate_count=1,
                    state="completed",
                )
            )
            await session.flush()
            session.add(
                Execution(
                    id="execution",
                    task_id="task",
                    candidate_index=0,
                    client_id="client",
                    tool_id="image.generate.zimage",
                    tool_version="1.0.0",
                    seed=123,
                    parameters={"prompt": "测试", "width": 1024, "seed": 123},
                    inputs=[],
                    execution_state="succeeded",
                    history=history(),
                    finished_at=event_time(9999999),
                )
            )
        self.worker = Worker(self.db, None, None, None, SimpleNamespace(data_dir=self.path))
        self.worker.collect = AsyncMock()
        self.worker.register_outputs = AsyncMock()

    async def asyncTearDown(self):
        await self.db.close()
        self.directory.cleanup()

    async def test_restart_recovery_and_repeated_collection_keep_real_times(self):
        for _ in range(2):
            await self.worker.complete_from_history("execution", history())
        async with self.db.session() as session:
            row = await session.get(Execution, "execution")
            self.assertEqual(row.started_at, event_time(1000000))
            self.assertEqual(row.finished_at, event_time(1060500))

    async def test_repeated_collection_without_history_timestamps_keeps_end(self):
        value = history()
        value["status"]["messages"] = []
        await self.worker.complete_from_history("execution", value)
        await self.worker.complete_from_history("execution", value)
        async with self.db.session() as session:
            row = await session.get(Execution, "execution")
            self.assertEqual(row.finished_at, event_time(9999999))

    async def test_list_and_detail_derive_times_for_old_records_and_keep_parameters(self):
        registry = SimpleNamespace(get=lambda *_: {"name": "快速文生图"})
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=self.db, registry=registry)))
        listing = await tasks(request, project_id="project", limit=80)
        detail = await task_detail(request, "task")
        for item in (listing[0]["executions"][0], detail["executions"][0]):
            self.assertEqual(item["timing"]["elapsed_seconds"], 60.5)
            self.assertEqual(item["parameters"]["prompt"], "测试")
        self.assertNotIn("history", listing[0]["executions"][0])
        self.assertIn("history", detail["executions"][0])


if __name__ == "__main__":
    unittest.main()
