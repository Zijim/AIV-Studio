import asyncio
import json
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

import httpx
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select
from test_creative_agent import AgentTestCase, plan, response

from app.api import router
from app.models import Artifact, Asset, Conversation, Execution, Task, uid
from app.schemas import ExecutePlan, ManualPlan, MessageRequest, RevisePlan

TOOL_ID = "image.generate.zimage"


class ImageAssetNameTests(AgentTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.image_format = "PNG"
        self.state.worker.comfy = SimpleNamespace(download=self.download_image)

    async def download_image(self, reference, target, *_):
        color = tuple(sha256(reference["subfolder"].encode()).digest()[:3])
        Image.new("RGB", (16, 16), color).save(target, self.image_format)

    async def finish_image(self, execution):
        tool = self.state.registry.get(execution.tool_id, execution.tool_version)
        reference = {"filename": "result_00001_.png", "subfolder": f"AIVAgent/{execution.id}", "type": "output"}
        await self.state.worker.complete_from_history(
            execution.id,
            {"status": {"status_str": "success"}, "outputs": {tool["prefixes"][0]: {"images": [reference]}}},
        )
        async with self.db.session() as session:
            artifact = await session.scalar(select(Artifact).where(Artifact.execution_id == execution.id))
            self.assertEqual(artifact.state, "saved", artifact.error)
            self.assertEqual(artifact.reference, reference)
            return await session.get(Asset, artifact.asset_id)

    async def execute_plan(self, draft):
        result = await self.state.plans.execute(
            draft["id"],
            ExecutePlan(revision=draft["revision"], plan_hash=draft["plan_hash"], idempotency_key=uid()),
        )
        async with self.db.session() as session:
            return list(
                await session.scalars(
                    select(Execution).where(Execution.task_id == result["task_id"]).order_by(Execution.candidate_index)
                )
            )

    async def test_autonomous_targets_name_candidates_and_keep_names_on_recollection(self):
        titles = {"mage": "法师三视图", "knight": "骑士三视图", "archer": "弓手三视图"}
        work_plan = plan()
        work_plan["creation_targets"] = [
            {
                "id": key,
                "text": f"Create the {key}",
                "media_type": "image",
                "requirement_ids": ["subject"],
                "asset_title": title,
            }
            for key, title in titles.items()
        ]
        status_update = deepcopy(work_plan)
        status_update["creation_targets"][0].pop("asset_title")
        status_update["creation_targets"][1]["asset_title"] = "改写计划时的新标题"
        steps = iter(
            (
                response(("plan", "update_plan", work_plan)),
                response(
                    *[
                        (
                            key,
                            "invoke_creation",
                            {
                                "capability_id": TOOL_ID,
                                "operation_id": key,
                                "purpose": f"Create the {key}",
                                "prompt_document": {"positive_prompt": f"Character turnaround of a {key}"},
                                "candidate_count": 2 if key == "mage" else 1,
                            },
                        )
                        for key in titles
                    ]
                ),
                response(("progress", "update_plan", status_update)),
            )
        )

        async def provider(_):
            step = next(steps, None)
            if step:
                return step
            tasks = [call.result["data"]["task_id"] for call in await self.calls(rid) if call.name == "invoke_creation"]
            return response(("wait", "wait_for_tasks", {"task_ids": tasks}))

        self.agent.provider = provider
        rid = await self.start("生成法师、骑士、弓手三视图，给法师两个候选")
        await self.agent.drive(rid)
        run = await self.run_record(rid)
        self.assertEqual(run.state, "waiting_task", run.stop_reason)
        self.assertEqual(
            {target["id"]: target["asset_title"] for target in run.work_plan["production_operations"]},
            titles,
        )
        async with self.db.session() as session:
            executions = list(
                await session.scalars(
                    select(Execution)
                    .join(Task)
                    .where(Task.agent_run_id == rid)
                    .order_by(Task.queue_seq, Execution.candidate_index)
                )
            )
        self.assertEqual(
            [row.configuration["prompt_generation"]["asset_title"] for row in executions],
            ["法师三视图", "法师三视图", "骑士三视图", "弓手三视图"],
        )
        assets = [await self.finish_image(execution) for execution in executions]
        self.assertEqual(
            [asset.name for asset in assets],
            [
                "法师三视图.png",
                "法师三视图-02.png",
                "骑士三视图.png",
                "弓手三视图.png",
            ],
        )

        original = assets[0]
        _, _, path = await self.state.storage.blob_path(original.id)
        path.unlink()
        await self.state.worker.collect(executions[0].id)
        restored, _, path = await self.state.storage.blob_path(original.id)
        self.assertEqual(restored.name, original.name)
        app = FastAPI()
        app.state.storage = self.state.storage
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            download = await client.get(f"/api/v1/assets/{original.id}/content?download=true")
        self.assertEqual(download.status_code, 200)
        self.assertIn(original.name, unquote(download.headers["content-disposition"]))
        self.assertEqual(download.content, path.read_bytes())

    async def test_assisted_name_survives_prompt_rewrite_and_uses_actual_format(self):
        async with self.db.transaction() as session:
            conversation = Conversation(project_id=self.pid, mode="assisted")
            session.add(conversation)
            await session.flush()
        routing = {"kind": "route", "message_zh": "准备生成图片", "tool_id": TOOL_ID, "task_mode": "image_generate"}
        composing = {
            "kind": "propose",
            "message_zh": "已拟定图片方案",
            "task_mode": "image_generate",
            "prompt_document": {"positive_prompt": "A rain-soaked alley at night"},
            "asset_title": " 雨夜:巷口?.PNG ",
        }
        rewritten = {
            **composing,
            "asset_title": ["无效标题"],
            "prompt_document": {"positive_prompt": "Rainy alley at night"},
        }
        planner = self.state.planner
        with (
            patch.object(planner, "key", new=AsyncMock(return_value="test-key")),
            patch.object(
                planner, "invoke", side_effect=[json.dumps(value) for value in (routing, composing, rewritten)]
            ) as provider,
        ):
            original = (
                await planner.prepare(
                    self.pid,
                    MessageRequest(
                        content="生成一张雨夜巷口图", conversation_id=conversation.id, idempotency_key=uid()
                    ),
                )
            )["plan"]
            draft = (
                await planner.prepare(
                    self.pid,
                    MessageRequest(
                        content="只改写提示词",
                        conversation_id=conversation.id,
                        idempotency_key=uid(),
                        revise_plan=RevisePlan(
                            plan_id=original["id"], revision=original["revision"], rewrite_only=True
                        ),
                    ),
                )
            )["plan"]
        self.assertEqual(provider.await_count, 3)
        self.assertEqual(draft["prompt_metadata"]["asset_title"], composing["asset_title"])
        self.image_format = "JPEG"
        (execution,) = await self.execute_plan(draft)
        asset = await self.finish_image(execution)
        self.assertEqual(asset.name, "雨夜 巷口.jpg")

    async def test_concurrent_unnamed_candidates_get_distinct_tool_fallbacks(self):
        draft = await self.state.plans.create(
            self.pid, ManualPlan(tool_id=TOOL_ID, parameters={"prompt": "An untitled landscape"}, candidate_count=2)
        )
        executions = await self.execute_plan(draft)
        assets = await asyncio.gather(*(self.finish_image(execution) for execution in executions))
        self.assertEqual({asset.name for asset in assets}, {"快速文生图-01.png", "快速文生图-02.png"})
