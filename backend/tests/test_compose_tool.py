"""Integration tests for the composition tools against a real database and pixels.

These do not call the model or ComfyUI; they exercise authorization, evidence
gating, encoding, ingestion and the refusal paths.
"""

import array
import math
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import av
from sqlalchemy import select, text

from app.config import ROOT, Settings
from app.creative_agent_schema import Compose, ComposeClip, Frame
from app.creative_agent_tools import AgentTools
from app.db import Database
from app.errors import AppError
from app.models import (
    Asset,
    Base,
    Conversation,
    CreativeAgentCall,
    CreativeAgentRun,
    CreativeAgentStep,
    Project,
    uid,
)
from app.planner import Planner
from app.plans import PlanService
from app.registry import Registry
from app.storage import Storage
from app.worker import Worker


def make_clip(path, *, width=64, height=96, frames=24, frequency=440, audio=True):
    samples = frames * 32000 // 24
    with av.open(str(path), "w") as out:
        video = out.add_stream("libx264", rate=24)
        video.width, video.height, video.pix_fmt = width, height, "yuv420p"
        sound = out.add_stream("aac", rate=32000) if audio else None
        if sound is not None:
            sound.layout = "stereo"
        for index in range(frames):
            frame = av.VideoFrame(width, height, "yuv420p")
            for plane, value in zip(frame.planes, [100, 128, 128]):
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts, frame.time_base = index, Fraction(1, 24)
            for packet in video.encode(frame):
                out.mux(packet)
        for packet in video.encode():
            out.mux(packet)
        if sound is not None:
            for start in range(0, samples, 1024):
                size = min(1024, samples - start)
                frame = av.AudioFrame(format="fltp", layout="stereo", samples=size)
                frame.sample_rate, frame.pts, frame.time_base = 32000, start, Fraction(1, 32000)
                values = array.array(
                    "f", [0.1 * math.sin(2 * math.pi * frequency * i / 32000) for i in range(start, start + size)]
                ).tobytes()
                for plane in frame.planes:
                    plane.update(values + bytes(plane.buffer_size - len(values)))
                for packet in sound.encode(frame):
                    out.mux(packet)
            for packet in sound.encode():
                out.mux(packet)


WORK_PLAN = {
    "output_type": "video",
    "requirements": [{"id": "film", "text": "完整成片"}],
    "items": [{"id": "edit", "text": "合成成片"}],
    "creation_targets": [{"id": "final_film", "text": "成片", "media_type": "video", "requirement_ids": ["film"]}],
}


class ComposeToolTests(unittest.IsolatedAsyncioTestCase):
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
        worker = Worker(self.db, None, registry, storage, self.settings)
        state = SimpleNamespace(
            db=self.db,
            settings=self.settings,
            registry=registry,
            storage=storage,
            plans=plans,
            planner=planner,
            worker=worker,
        )
        self.storage = storage
        self.tools = AgentTools(state)
        async with self.db.transaction() as session:
            project = Project(name="compose tests")
            session.add(project)
            await session.flush()
            conversation = Conversation(project_id=project.id, mode="autonomous")
            session.add(conversation)
            await session.flush()
            self.pid, self.cid = project.id, conversation.id

    async def make_run(self, composes=6):
        async with self.db.transaction() as session:
            run = CreativeAgentRun(
                project_id=self.pid,
                conversation_id=self.cid,
                goal="合成测试",
                goal_snapshot={"original": "合成测试", "project": {}},
                scope={
                    "selected": [],
                    "use_key_assets": True,
                    "image_roles": {},
                    "reference_policy": {"state": "resolved", "mode": "available"},
                },
                access={"public_access": False, "can_manage": True},
                limits={
                    "executions": 60,
                    "issue_reworks": 6,
                    "model_requests": 200,
                    "tool_calls": 400,
                    "no_progress": 3,
                    "active_ms": 10_800_000,
                    "composes": composes,
                },
                state="running",
                work_plan=WORK_PLAN,
            )
            session.add(run)
            await session.flush()
            return run.id

    async def run_record(self, run_id):
        async with self.db.session() as session:
            return await session.get(CreativeAgentRun, run_id)

    async def add_call(self, run_id, name, *, result=None, state="completed", position=0):
        sequence = len(await self.calls(run_id)) + 1
        async with self.db.transaction() as session:
            step = CreativeAgentStep(run_id=run_id, sequence=sequence, control_version=1, state="completed")
            session.add(step)
            await session.flush()
            call = CreativeAgentCall(
                run_id=run_id,
                step_id=step.id,
                call_id=uid(),
                position=position,
                name=name,
                arguments="{}",
                state=state,
                result=result,
            )
            session.add(call)
            await session.flush()
            return call.id

    async def calls(self, run_id):
        async with self.db.session() as session:
            return (
                await session.scalars(select(CreativeAgentCall).where(CreativeAgentCall.run_id == run_id))
            ).all()

    async def add_clip(self, run_id, *, width=64, height=96, frames=24, audio=True):
        folder = self.settings.data_dir / "staging"
        path = folder / (uid() + ".mp4")
        make_clip(path, width=width, height=height, frames=frames, audio=audio)
        asset_id = await self.storage.ingest(path, path.name, self.pid, origin="generated", role="final")
        async with self.db.transaction() as session:
            run = await session.get(CreativeAgentRun, run_id)
            run.scope = {**run.scope, "selected": [*run.scope.get("selected", []), asset_id]}
        async with self.db.session() as session:
            asset = await session.get(Asset, asset_id)
            return asset_id, asset.blob_id

    async def observe(self, run_id, asset_id, blob_id):
        return await self.add_call(
            run_id,
            "inspect_media",
            result={
                "ok": True,
                "data": {
                    "asset_id": asset_id,
                    "blob_id": blob_id,
                    "technical": {"decode": "complete"},
                    "frames": [{"file": "0.jpg", "frame_index": 0}],
                },
            },
        )

    async def compose(self, run_id, call_id, clips, target_frames=None, dry_run=False):
        request = Compose(
            operation_id="final_film",
            clips=[ComposeClip(**clip) for clip in clips],
            target_frames=target_frames,
            output_name="成片",
            dry_run=dry_run,
        )
        return await self.tools.compose_video(
            await self.run_record(run_id), SimpleNamespace(id=call_id), request, 1
        )

    async def test_natural_length_uses_the_sum_of_clip_frames(self):
        run_id = await self.make_run()
        first, first_blob = await self.add_clip(run_id, frames=24)
        second, second_blob = await self.add_clip(run_id, frames=24)
        await self.observe(run_id, first, first_blob)
        await self.observe(run_id, second, second_blob)
        call_id = await self.add_call(run_id, "compose_video", state="running")
        result = await self.compose(
            run_id,
            call_id,
            [
                {"asset_id": first, "in_frame": 0, "frames": 24},
                {"asset_id": second, "in_frame": 0, "frames": 24},
            ],
        )
        self.assertTrue(result["data"]["natural"])
        self.assertEqual(result["data"]["frames"], 48)
        self.assertEqual(result["data"]["duration"], 2.0)

    async def test_composes_observed_clips_into_one_asset(self):
        run_id = await self.make_run()
        first, first_blob = await self.add_clip(run_id, frames=24)
        second, second_blob = await self.add_clip(run_id, frames=24)
        await self.observe(run_id, first, first_blob)
        await self.observe(run_id, second, second_blob)
        call_id = await self.add_call(run_id, "compose_video", state="running")
        result = await self.compose(
            run_id,
            call_id,
            [
                {"asset_id": first, "in_frame": 0, "frames": 24},
                {"asset_id": second, "in_frame": 0, "frames": 24},
            ],
            target_frames=48,
        )
        self.assertEqual(result["data"]["frames"], 48)
        self.assertEqual(result["data"]["duration"], 2.0)
        asset_id = result["data"]["asset_id"]
        async with self.db.session() as session:
            asset = await session.get(Asset, asset_id)
            self.assertEqual(asset.origin, "composition")
            self.assertEqual(asset.media_type, "video")
            composition = asset.source_info["composition"]
            self.assertEqual(composition["frames"], 48)
            self.assertEqual([clip["asset_id"] for clip in composition["clips"]], [first, second])
            call = await session.get(CreativeAgentCall, call_id)
            self.assertEqual(call.asset_id, asset_id)

    async def test_unobserved_clip_is_refused(self):
        run_id = await self.make_run()
        clip, _ = await self.add_clip(run_id, )
        call_id = await self.add_call(run_id, "compose_video", state="running")
        with self.assertRaises(AppError) as raised:
            await self.compose(run_id, call_id, [{"asset_id": clip, "in_frame": 0, "frames": 24}], 24)
        self.assertEqual(raised.exception.code, "compose_source_unchecked")
        async with self.db.session() as session:
            self.assertEqual(
                await session.scalar(select(Asset).where(Asset.origin == "composition")), None
            )

    async def test_mismatched_geometry_is_refused_before_encoding(self):
        run_id = await self.make_run()
        wide, wide_blob = await self.add_clip(run_id, width=96, height=64)
        tall, tall_blob = await self.add_clip(run_id, )
        await self.observe(run_id, wide, wide_blob)
        await self.observe(run_id, tall, tall_blob)
        call_id = await self.add_call(run_id, "compose_video", state="running")
        with self.assertRaises(AppError) as raised:
            await self.compose(
                run_id,
                call_id,
                [
                    {"asset_id": wide, "in_frame": 0, "frames": 24},
                    {"asset_id": tall, "in_frame": 0, "frames": 24},
                ],
                48,
            )
        self.assertEqual(raised.exception.code, "media_mismatch")

    async def test_compose_limit_stops_after_the_configured_count(self):
        run_id = await self.make_run(composes=1)
        clip, blob = await self.add_clip(run_id, )
        await self.observe(run_id, clip, blob)
        await self.add_call(run_id, "compose_video", state="running", position=0)
        second = await self.add_call(run_id, "compose_video", state="running", position=1)
        with self.assertRaises(AppError) as raised:
            await self.compose(run_id, second, [{"asset_id": clip, "in_frame": 0, "frames": 24}], 24)
        self.assertEqual(raised.exception.code, "compose_limit")

    async def test_dry_run_reports_budget_without_creating_an_asset(self):
        run_id = await self.make_run()
        clip, blob = await self.add_clip(run_id, )
        await self.observe(run_id, clip, blob)
        call_id = await self.add_call(run_id, "compose_video", state="running")
        with self.assertRaises(AppError) as raised:
            await self.compose(run_id, call_id, [{"asset_id": clip, "in_frame": 0, "frames": 24}], 30, dry_run=True)
        self.assertEqual(raised.exception.code, "frame_mismatch")
        self.assertEqual(raised.exception.details["difference"], -6)
        result = await self.compose(
            run_id, call_id, [{"asset_id": clip, "in_frame": 0, "frames": 24}], 24, dry_run=True
        )
        self.assertTrue(result["data"]["dry_run"])
        self.assertEqual(result["data"]["frames"], 24)

    async def test_extract_last_frame_becomes_an_image_asset(self):
        run_id = await self.make_run()
        clip, _ = await self.add_clip(run_id, frames=12)
        call_id = await self.add_call(run_id, "extract_frame", state="running")
        result = await self.tools.extract_frame(
            await self.run_record(run_id),
            SimpleNamespace(id=call_id),
            Frame(asset_id=clip, frame_index=-1, name="末帧"),
            1,
        )
        self.assertEqual(result["data"]["frame_index"], 11)
        self.assertEqual(result["data"]["media_type"], "image")
        async with self.db.session() as session:
            asset = await session.get(Asset, result["data"]["asset_id"])
            self.assertEqual(asset.origin, "agent_frame")
            self.assertEqual(asset.source_info["frame_index"], 11)

    async def test_extract_frame_beyond_the_end_is_refused(self):
        run_id = await self.make_run()
        clip, _ = await self.add_clip(run_id, frames=12)
        call_id = await self.add_call(run_id, "extract_frame", state="running")
        with self.assertRaises(AppError) as raised:
            await self.tools.extract_frame(
                await self.run_record(run_id),
                SimpleNamespace(id=call_id),
                Frame(asset_id=clip, frame_index=50, name="越界"),
                1,
            )
        self.assertEqual(raised.exception.code, "frame_range")


if __name__ == "__main__":
    unittest.main()
