import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from app.config import Settings
from app.vision import linked_asset_ids


class PublicAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_app_public_boundary(self):
        from app.main import app, lifespan

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "AIV_DATA_DIR": directory,
                    "AIV_PUBLIC_ORIGIN": "https://studio.zijim.xyz",
                    "AIV_COMFY_URL": "http://127.0.0.1:1",
                },
            ),
        ):
            app.middleware_stack = None
            async with lifespan(app):
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
                async with (
                    httpx.AsyncClient(transport=transport, base_url="https://studio.zijim.xyz") as public,
                    httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as local,
                ):
                    session = await public.get("/api/v1/session")
                    self.assertEqual(session.status_code, 200)
                    self.assertTrue(session.json()["public_access"])
                    self.assertFalse(session.json()["can_manage"])
                    self.assertIn("Secure", session.headers["set-cookie"])
                    self.assertIn("HttpOnly", session.headers["set-cookie"])
                    self.assertEqual(session.headers["cache-control"], "no-store")
                    public_csrf = session.json()["csrf_token"]
                    headers = {"Origin": "https://studio.zijim.xyz", "X-AIV-CSRF": public_csrf}
                    private_session = await local.get("/api/v1/session")
                    self.assertTrue(private_session.json()["can_manage"])
                    self.assertNotEqual(private_session.json()["csrf_token"], public_csrf)
                    self.assertNotIn("Secure", private_session.headers["set-cookie"])
                    for verb in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"):
                        for path in (
                            "/api/v1/settings",
                            "/api/v1/settings/credentials",
                            "/api/v1/settings/llm-key",
                            "/api/v1/system/shutdown",
                            "/api/v1/backups",
                            "/api/v1/backups/restore",
                            "/api/v1/gc/preview",
                            "/api/v1/cleanup/execute",
                            "/api/v1/comfy-output-files/content",
                            "/api/v1/imports/comfy-output",
                            "/api/v1/tools/refresh",
                            "/api/v1/diagnostics",
                            "/api/v1/resources",
                            "/docs",
                            "/openapi.json",
                        ):
                            with self.subTest(method=verb, path=path):
                                response = await public.request(verb, path, headers=headers)
                                self.assertEqual(response.status_code, 403)
                    self.assertEqual((await local.get("/api/v1/settings")).status_code, 200)
                    health = (await public.get("/api/v1/health")).json()
                    self.assertNotIn("data_dir", health)
                    self.assertNotIn("workspace_root", health)
                    self.assertNotIn("process_id", health)
                    for extra in (
                        {"Host": "evil.example"},
                        {"Origin": "https://evil.example"},
                        {"Origin": "http://127.0.0.1:8765"},
                    ):
                        self.assertEqual((await public.get("/api/v1/projects", headers=extra)).status_code, 403)
                    for csrf in ("", private_session.json()["csrf_token"]):
                        response = await public.post(
                            "/api/v1/projects", headers={**headers, "X-AIV-CSRF": csrf}, json={"name": "rejected"}
                        )
                        self.assertEqual(response.status_code, 403)
                    response = await public.post("/api/v1/projects", headers=headers, json={"name": "共享验证"})
                    self.assertEqual(response.status_code, 201)
                    project_id = response.json()["id"]
                    self.assertEqual((await public.get(f"/api/v1/projects/{project_id}")).status_code, 200)
                    self.assertEqual(
                        (await local.get("/api/v1/settings", headers={"Cf-Connecting-IP": "203.0.113.1"})).status_code,
                        403,
                    )
                    remote_transport = httpx.ASGITransport(app=app, client=("203.0.113.2", 12345))
                    async with httpx.AsyncClient(
                        transport=remote_transport, base_url="http://localhost:8765"
                    ) as remote:
                        self.assertEqual((await remote.get("/api/v1/settings")).status_code, 403)
                    response = await public.post(
                        f"/api/v1/projects/{project_id}/imports",
                        headers={**headers, "Content-Length": str(100 * 1024**2)},
                        content=b"small",
                    )
                    self.assertEqual(response.status_code, 413)
                    app.state.settings.public_max_request_bytes = 1024

                    async def oversized_stream():
                        yield b'{"name":"'
                        yield b"x" * 1025
                        yield b'"}'

                    response = await public.post(
                        "/api/v1/projects",
                        headers={**headers, "Content-Type": "application/json"},
                        content=oversized_stream(),
                    )
                    self.assertEqual(response.status_code, 413)
                    app.state.settings.public_writes_per_minute = 1
                    for expected in (403, 429):
                        response = await public.post(
                            "/api/v1/projects", headers={"Cf-Connecting-IP": "203.0.113.250"}, json={"name": "rate"}
                        )
                        self.assertEqual(response.status_code, expected)
                    app.state.settings.public_origin = ""
                    self.assertEqual((await public.get("/api/v1/session")).status_code, 403)


class PublicConfigurationTests(unittest.TestCase):
    def test_origin_is_explicit_https(self):
        self.assertEqual(Settings(public_origin="https://Studio.Zijim.xyz/").public_origin, "https://studio.zijim.xyz")
        for origin in ("http://studio.zijim.xyz", "https://studio.zijim.xyz/path", "https://user@studio.zijim.xyz"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                Settings(public_origin=origin)

    def test_only_workspace_asset_links_attach(self):
        asset = "12345678-1234-1234-1234-123456789012"
        route = f"/api/v1/assets/{asset}/content"
        for prefix, expected in (
            ("https://studio.zijim.xyz", [asset]),
            ("https://evil.example", []),
            ("https://studio.zijim.xyz.evil.example", []),
            ("http://127.0.0.1:8765", [asset]),
            ("http://127.0.0.1:8766", []),
            ("", [asset]),
        ):
            with self.subTest(prefix=prefix):
                self.assertEqual(linked_asset_ids(prefix + route, 8765, "https://studio.zijim.xyz"), expected)
