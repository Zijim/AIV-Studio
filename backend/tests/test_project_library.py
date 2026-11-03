import os
import tempfile
import unittest
from unittest.mock import patch

import httpx


class ProjectLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(
            os.environ,
            {
                "AIV_DATA_DIR": self.directory.name,
                "AIV_COMFY_URL": "http://127.0.0.1:1",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        from app.main import app, lifespan

        app.middleware_stack = None
        self.app = app
        self.lifecycle = lifespan(app)
        await self.lifecycle.__aenter__()
        self.addAsyncCleanup(self.lifecycle.__aexit__, None, None, None)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        self.client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")
        self.addAsyncCleanup(self.client.aclose)
        session = await self.client.get("/api/v1/session")
        self.assertEqual(session.status_code, 200)
        self.headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-AIV-CSRF": session.json()["csrf_token"],
        }

    async def project(self, name="素材库测试"):
        response = await self.client.post(
            "/api/v1/projects", headers=self.headers, json={"name": name}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    async def imported(self, project_id, name="note.txt", body=b"hello"):
        response = await self.client.post(
            f"/api/v1/projects/{project_id}/imports",
            headers=self.headers,
            files=[("files", (name, body, "text/plain"))],
        )
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual(item["state"], "saved", response.text)
        return item["asset_id"]

    async def tag(self, project_id, name, color=None):
        response = await self.client.post(
            f"/api/v1/projects/{project_id}/tags",
            headers=self.headers,
            json={"name": name, **({"color": color} if color else {})},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_key_assets_tags_and_facets(self):
        project_id = await self.project()
        asset_id = await self.imported(project_id)
        people = await self.tag(project_id, "人物")
        scene = await self.tag(project_id, "场景")

        response = await self.client.post(
            f"/api/v1/projects/{project_id}/key-assets",
            headers=self.headers,
            json={"asset_ids": [asset_id], "tag_ids": [people["id"], scene["id"]]},
        )
        self.assertEqual(response.status_code, 200, response.text)

        key = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?curated=true", headers=self.headers
        )
        self.assertEqual(key.status_code, 200, key.text)
        self.assertEqual(key.json()["total"], 1)
        item = key.json()["items"][0]
        self.assertTrue(item["project"]["curated"])
        self.assertEqual({tag["name"] for tag in item["project"]["tags"]}, {"人物", "场景"})

        by_tag = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?tag_id={people['id']}", headers=self.headers
        )
        self.assertEqual(by_tag.status_code, 200, by_tag.text)
        self.assertEqual(by_tag.json()["total"], 1)

        facets = await self.client.get(
            f"/api/v1/projects/{project_id}/library/facets", headers=self.headers
        )
        self.assertEqual(facets.status_code, 200, facets.text)
        body = facets.json()
        self.assertEqual(body["all"], 1)
        self.assertEqual(body["key"], 1)
        self.assertEqual(body["uncurated"], 0)
        counts = {tag["name"]: tag["count"] for tag in body["tags"]}
        self.assertEqual(counts, {"人物": 1, "场景": 1})

        renamed = await self.client.patch(
            f"/api/v1/projects/{project_id}/tags/{people['id']}",
            headers=self.headers,
            json={"name": "角色", "color": "#8fb7a8"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "角色")

        search = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?q=角色", headers=self.headers
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertEqual(search.json()["total"], 1)

        cleared = await self.client.put(
            f"/api/v1/projects/{project_id}/assets/{asset_id}/tags",
            headers=self.headers,
            json={"tag_ids": []},
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        key = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?curated=true", headers=self.headers
        )
        self.assertEqual(key.json()["items"][0]["project"]["tags"], [])
        facets = await self.client.get(
            f"/api/v1/projects/{project_id}/library/facets", headers=self.headers
        )
        self.assertEqual({tag["name"]: tag["count"] for tag in facets.json()["tags"]}, {"角色": 0, "场景": 0})

        removed = await self.client.post(
            f"/api/v1/projects/{project_id}/key-assets/remove",
            headers=self.headers,
            json={"asset_ids": [asset_id]},
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        key = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?curated=true", headers=self.headers
        )
        self.assertEqual(key.json()["total"], 0)
        all_items = await self.client.get(
            f"/api/v1/projects/{project_id}/assets", headers=self.headers
        )
        self.assertEqual(all_items.json()["total"], 1)

    async def test_tag_lifecycle_and_project_boundaries(self):
        project_a = await self.project("A")
        project_b = await self.project("B")
        asset_a = await self.imported(project_a, "a.txt")
        tag_a = await self.tag(project_a, "Hero")

        duplicate = await self.client.post(
            f"/api/v1/projects/{project_a}/tags",
            headers=self.headers,
            json={"name": "hero"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

        foreign = await self.client.post(
            f"/api/v1/projects/{project_b}/key-assets",
            headers=self.headers,
            json={"asset_ids": [asset_a], "tag_ids": [tag_a["id"]]},
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)

        add = await self.client.post(
            f"/api/v1/projects/{project_a}/tags/{tag_a['id']}/assets",
            headers=self.headers,
            json={"asset_ids": [asset_a]},
        )
        self.assertEqual(add.status_code, 200, add.text)

        deleted = await self.client.delete(
            f"/api/v1/projects/{project_a}/tags/{tag_a['id']}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        key = await self.client.get(
            f"/api/v1/projects/{project_a}/assets?curated=true", headers=self.headers
        )
        self.assertEqual(key.json()["total"], 1)
        self.assertEqual(key.json()["items"][0]["project"]["tags"], [])

        archived = await self.client.post(
            f"/api/v1/projects/{project_a}/archive", headers=self.headers
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        blocked = await self.client.post(
            f"/api/v1/projects/{project_a}/key-assets",
            headers=self.headers,
            json={"asset_ids": [asset_a]},
        )
        self.assertIn(blocked.status_code, (403, 409), blocked.text)

    async def test_trash_preserves_key_state(self):
        project_id = await self.project()
        asset_id = await self.imported(project_id)
        await self.client.post(
            f"/api/v1/projects/{project_id}/key-assets",
            headers=self.headers,
            json={"asset_ids": [asset_id]},
        )
        removed = await self.client.delete(
            f"/api/v1/projects/{project_id}/assets/{asset_id}", headers=self.headers
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        key = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?curated=true", headers=self.headers
        )
        self.assertEqual(key.json()["total"], 0)
        trash = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?removed=true", headers=self.headers
        )
        self.assertEqual(trash.json()["total"], 1)
        restored = await self.client.post(
            f"/api/v1/projects/{project_id}/assets/{asset_id}/restore", headers=self.headers
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        key = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?curated=true", headers=self.headers
        )
        self.assertEqual(key.json()["total"], 1)

    async def test_project_scoped_display_name(self):
        project_a = await self.project("A")
        project_b = await self.project("B")
        asset_id = await self.imported(project_a, "portrait.txt", b"portrait")
        renamed = await self.client.patch(
            f"/api/v1/projects/{project_a}/assets/{asset_id}",
            headers=self.headers,
            json={"name": "  主角定妆  "},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)

        listed = await self.client.get(
            f"/api/v1/projects/{project_a}/assets", headers=self.headers
        )
        item = listed.json()["items"][0]
        self.assertEqual(item["name"], "主角定妆")
        self.assertEqual(item["original_name"], "portrait.txt")
        self.assertEqual(item["project"]["display_name"], "主角定妆")

        attached = await self.client.post(
            f"/api/v1/projects/{project_b}/assets/{asset_id}/attach",
            headers=self.headers,
        )
        self.assertEqual(attached.status_code, 200, attached.text)
        other = await self.client.get(
            f"/api/v1/projects/{project_b}/assets", headers=self.headers
        )
        self.assertEqual(other.json()["items"][0]["name"], "portrait.txt")
        self.assertEqual(other.json()["items"][0]["original_name"], "portrait.txt")

        search = await self.client.get(
            f"/api/v1/projects/{project_a}/assets?q=主角定妆", headers=self.headers
        )
        self.assertEqual(search.json()["total"], 1)

        cleared = await self.client.patch(
            f"/api/v1/projects/{project_a}/assets/{asset_id}",
            headers=self.headers,
            json={"name": ""},
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        listed = await self.client.get(
            f"/api/v1/projects/{project_a}/assets", headers=self.headers
        )
        self.assertEqual(listed.json()["items"][0]["name"], "portrait.txt")

    async def test_legacy_note_tags_promote_to_key_library(self):
        project_id = await self.project()
        asset_id = await self.imported(project_id)
        response = await self.client.patch(
            f"/api/v1/projects/{project_id}/assets/{asset_id}",
            headers=self.headers,
            json={"note": "主角", "tags": ["主角", "定妆"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        key = await self.client.get(
            f"/api/v1/projects/{project_id}/assets?curated=true", headers=self.headers
        )
        self.assertEqual(key.json()["total"], 1)
        self.assertEqual(
            {tag["name"] for tag in key.json()["items"][0]["project"]["tags"]},
            {"主角", "定妆"},
        )
