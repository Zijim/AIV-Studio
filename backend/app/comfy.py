import asyncio
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
import websockets


class ComfyClient:
    def __init__(self, base_url, output_root=None):
        self.base_url = base_url.rstrip("/")
        self.output_root = Path(output_root).resolve() if output_root else None
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=10, trust_env=False)

    async def get(self, route):
        response = await self.http.get(route)
        response.raise_for_status()
        return response.json()

    async def queue(self):
        return await self.get("/queue")

    async def history(self, prompt_id):
        return (await self.get(f"/history/{prompt_id}")).get(prompt_id)

    async def upload(self, path: Path, blob_id, extension, subfolder):
        name = blob_id + extension
        with path.open("rb") as file:
            response = await self.http.post(
                "/upload/image",
                files={"image": (name, file, mimetypes.guess_type(name)[0] or "image/png")},
                data={"type": "input", "subfolder": subfolder, "overwrite": "false"},
                timeout=60,
            )
        response.raise_for_status()
        result = response.json()
        return "/".join(x for x in (result.get("subfolder", ""), result["name"]) if x)

    async def submit(self, payload):
        # Never automatically retry this endpoint: response loss != rejection.
        response = await self.http.post("/prompt", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    async def cancel(self, prompt_id):
        response = await self.http.post(f"/api/jobs/{prompt_id}/cancel", timeout=10)
        response.raise_for_status()
        return response.json()

    async def free(self):
        response = await self.http.post("/free", json={"unload_models": True, "free_memory": True})
        response.raise_for_status()

    async def download(self, reference, destination, max_bytes, check_space=None):
        try:
            return await self._http_download(reference, destination, max_bytes, check_space)
        except (httpx.HTTPError, OSError):
            if not self.output_root or reference.get("type", "output") != "output":
                raise
            source = (self.output_root / reference.get("subfolder", "") / reference["filename"]).resolve()
            if not source.is_relative_to(self.output_root) or not source.is_file():
                raise
            return await asyncio.to_thread(self._copy_local, source, destination, max_bytes, check_space)

    def _copy_local(self, source, destination, max_bytes, check_space):
        written = 0
        with source.open("rb") as src, destination.open("wb") as dest:
            while chunk := src.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("产物超过单文件保存上限。")
                if check_space:
                    check_space()
                dest.write(chunk)
            dest.flush()
            os.fsync(dest.fileno())
        return written

    async def _http_download(self, reference, destination, max_bytes, check_space=None):
        params = {k: reference.get(k, "") for k in ("filename", "subfolder", "type")}
        params["type"] = params["type"] or "output"
        written = 0
        async with self.http.stream("GET", "/view", params=params, timeout=60) as response:
            response.raise_for_status()
            with destination.open("wb") as file:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError("产物超过单文件保存上限。")
                    if check_space:
                        check_space()
                    await asyncio.to_thread(file.write, chunk)
                await asyncio.to_thread(file.flush)
                await asyncio.to_thread(os.fsync, file.fileno())
        return written

    async def connect(self, client_id):
        parsed = urlparse(self.base_url)
        url = ("wss" if parsed.scheme == "https" else "ws") + "://" + parsed.netloc + "/ws?clientId=" + client_id
        return await websockets.connect(url, max_size=64 * 1024**2, open_timeout=10, ping_interval=20)

    async def close(self):
        await self.http.aclose()


def file_references(outputs):
    for node_id, data in (outputs or {}).items():
        for field, items in data.items():
            if not isinstance(items, list):
                continue
            for index, value in enumerate(items):
                if isinstance(value, dict) and isinstance(value.get("filename"), str):
                    yield (
                        str(node_id),
                        field,
                        index,
                        {k: value.get(k, "output" if k == "type" else "") for k in ("filename", "subfolder", "type")},
                    )
