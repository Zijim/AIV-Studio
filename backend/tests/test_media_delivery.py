import asyncio
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select
from starlette.responses import JSONResponse

from app.api import router
from app.config import Settings
from app.db import Database
from app.errors import AppError
from app.maintenance import MaintenanceService
from app.media import inspect_file
from app.media_delivery import MEDIA_CACHE_CONTROL
from app.models import Asset, Base, Blob, Event, Project, ProjectAsset, now
from app.security import WorkspaceSecurityMiddleware
from app.storage import Storage

ASSET_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "22222222-2222-2222-2222-222222222222"


class MediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "previews").mkdir()
        (self.root / "blobs").mkdir()
        self.source = self.root / "blobs" / "source"
        image = Image.new("RGBA", (1000, 750), (125, 85, 190, 110))
        image.save(self.source, "PNG")
        self.original = self.source.read_bytes()
        self.sha = hashlib.sha256(self.original).hexdigest()
        self.preview = self.root / "previews" / (ASSET_ID + ".png")
        inspected = inspect_file(self.source, "source.png", self.preview)
        self.db = Database(self.root / "app.sqlite3")
        async with self.db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.db.transaction() as session:
            session.add(Project(id=PROJECT_ID, name="媒体缓存验证"))
            session.add(
                Blob(
                    sha256=self.sha,
                    byte_size=len(self.original),
                    relative_path="blobs/source",
                    mime_type="image/png",
                    extension=".png",
                )
            )
            await session.flush()
            session.add(
                Asset(
                    id=ASSET_ID,
                    blob_id=self.sha,
                    name="source.png",
                    media_type="image",
                    origin="imported",
                    preview_path=self.preview.name,
                    media_info=inspected["media_info"],
                )
            )
            await session.flush()
            session.add(ProjectAsset(project_id=PROJECT_ID, asset_id=ASSET_ID))
        self.app = FastAPI()
        self.app.include_router(router)
        state = self.app.state
        state.settings = Settings(data_dir=self.root, public_origin="https://studio.zijim.xyz")
        state.db = self.db
        state.storage = Storage(self.db, state.settings)
        state.public_session_token, state.public_csrf = "public-session", "public-csrf"
        state.session_token, state.csrf = "local-session", "local-csrf"
        self.app.add_middleware(WorkspaceSecurityMiddleware, state=state)

        @self.app.exception_handler(AppError)
        async def handle_error(request, error):
            return JSONResponse({"error": {"code": error.code}}, status_code=error.status)

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://studio.zijim.xyz"
        )
        self.base = f"/api/v1/assets/{ASSET_ID}"

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.db.close()
        self.directory.cleanup()

    async def test_existing_png_becomes_small_transparent_webp_without_changing_sources(self):
        before = self.preview.read_bytes()
        response = await self.client.get(self.base + "/preview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/webp")
        with Image.open(io.BytesIO(response.content)) as preview:
            self.assertLessEqual(max(preview.size), 320)
            self.assertEqual(preview.format, "WEBP")
            self.assertEqual(preview.getpixel((0, 0))[3], 110)
        self.assertEqual(self.preview.read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), self.original)

    async def test_media_revalidates_without_retransferring_payload(self):
        for endpoint in ("/preview", "/content"):
            first = await self.client.get(self.base + endpoint)
            self.assertEqual(first.headers["cache-control"], MEDIA_CACHE_CONTROL)
            etag = first.headers["etag"]
            for condition in (etag, "W/" + etag, '"unrelated", ' + etag, "*"):
                with self.subTest(endpoint=endpoint, condition=condition):
                    cached = await self.client.get(self.base + endpoint, headers={"If-None-Match": condition})
                    self.assertEqual(cached.status_code, 304)
                    self.assertEqual(cached.content, b"")
                    self.assertEqual(cached.headers["etag"], etag)
                    self.assertEqual(cached.headers["cache-control"], MEDIA_CACHE_CONTROL)
            head = await self.client.head(self.base + endpoint)
            self.assertEqual(head.status_code, 200)
            self.assertEqual(int(head.headers["content-length"]), len(first.content))
            self.assertEqual(head.content, b"")

    async def test_original_range_and_download_still_work(self):
        ranged = await self.client.get(self.base + "/content", headers={"Range": "bytes=0-31"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, self.original[:32])
        self.assertEqual(ranged.headers["cache-control"], MEDIA_CACHE_CONTROL)
        downloaded = await self.client.get(self.base + "/content?download=true")
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertEqual(downloaded.content, self.original)

    async def test_missing_original_can_still_use_preview_but_not_stale_original(self):
        first = await self.client.get(self.base + "/content")
        self.source.unlink()
        missing = await self.client.get(self.base + "/content", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.headers["cache-control"], "no-store")
        self.assertEqual((await self.client.get(self.base + "/preview")).status_code, 200)

    async def test_missing_preview_source_cannot_be_served_from_an_old_derivative(self):
        first = await self.client.get(self.base + "/preview")
        self.preview.unlink()
        missing = await self.client.get(self.base + "/preview", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.headers["cache-control"], "no-store")

    async def test_preview_revision_changes_etag(self):
        first = await self.client.get(self.base + "/preview")
        Image.new("RGB", (640, 640), "red").save(self.preview, "PNG")
        updated = await self.client.get(self.base + "/preview", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(updated.status_code, 200)
        self.assertNotEqual(updated.headers["etag"], first.headers["etag"])
        self.assertNotEqual(updated.content, first.content)

    async def test_concurrent_requests_share_one_derivative(self):
        responses = await asyncio.gather(*(self.client.get(self.base + "/preview") for _ in range(5)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len({response.headers["etag"] for response in responses}), 1)
        self.assertEqual(len(list(self.preview.parent.glob("*.webp"))), 1)

    async def test_encoder_failure_falls_back_to_existing_preview(self):
        with patch.object(Image.Image, "save", side_effect=OSError("encoder unavailable")):
            response = await self.client.get(self.base + "/preview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, self.preview.read_bytes())
        self.assertFalse(list(self.preview.parent.glob("*.partial")))

    async def test_gc_invalidates_original_preview_and_derivatives(self):
        original = await self.client.get(self.base + "/content")
        preview = await self.client.get(self.base + "/preview")
        other = self.preview.parent / "another.thumb-v1.123.webp"
        other.write_bytes(b"unrelated")
        async with self.db.transaction() as session:
            membership = await session.get(ProjectAsset, (PROJECT_ID, ASSET_ID))
            membership.removed_at = now()
        maintenance = MaintenanceService(self.db, self.app.state.settings, self.app.state.storage)
        job = await maintenance.preview_gc()
        result = await maintenance.gc(job["id"], job["data"]["hash"])
        self.assertEqual(result["removed_files"], 1)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.preview.exists())
        self.assertFalse(list(self.preview.parent.glob(ASSET_ID + ".*.webp")))
        self.assertTrue(other.exists())
        for suffix, first, expected in (("/content", original, 409), ("/preview", preview, 404)):
            response = await self.client.get(self.base + suffix, headers={"If-None-Match": first.headers["etag"]})
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.headers["cache-control"], "no-store")
        detail = (await self.client.get(self.base)).json()
        self.assertIsNone(detail["preview_url"])
        self.assertEqual(detail["presence"], "purged")
        async with self.db.session() as session:
            event = await session.scalar(select(Event).where(Event.type == "asset.purged"))
            self.assertEqual(event.entity_id, ASSET_ID)
