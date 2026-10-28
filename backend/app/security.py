"""Keep local administration separate from an optional anonymous shared site."""

import ipaddress
import re
import secrets
import time
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ADMIN_PATH = re.compile(
    r"^/api/v1/(?:settings|diagnostics|system|backups|gc|cleanup|comfy-output-files|resources)(?:/|$)"
)


class WorkspaceSecurityMiddleware:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.buckets = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        request_state = scope.setdefault("state", {})
        request_state["request_id"] = secrets.token_hex(8)

        async def reject(code, message, status=403, extra_headers=None):
            response = JSONResponse(
                {"error": {"code": code, "message": message}}, status_code=status, headers=extra_headers
            )
            await response(scope, receive, send)

        settings = self.state.settings
        public_origin = settings.public_origin
        public_host = urlsplit(public_origin).netloc if public_origin else ""
        authority = headers.get("host", "").lower()
        local_authorities = {
            name + suffix for name in ("localhost", "127.0.0.1") for suffix in ("", f":{settings.port}", ":5173")
        }
        public = bool(public_host and authority == public_host)
        if not public:
            try:
                local_client = ipaddress.ip_address((scope.get("client") or ("", 0))[0]).is_loopback
            except ValueError:
                local_client = False
            # Tunnel traffic must retain its public Host. Never grant administration
            # because cloudflared happens to connect from the loopback interface.
            if (
                authority not in local_authorities
                or not local_client
                or any(name in headers for name in ("cf-ray", "cf-connecting-ip", "cf-access-jwt-assertion"))
            ):
                await reject("host_rejected", "请求域名不受信任。")
                return
        request_state["public_access"] = public
        request_state["can_manage"] = not public
        allowed_origins = (
            {public_origin}
            if public
            else {f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (settings.port, 5173)}
        )
        origin = headers.get("origin")
        if origin and origin not in allowed_origins:
            await reject("origin_rejected", "请求来源不受信任。")
            return
        path, method = scope["path"], scope["method"]
        if public and (
            ADMIN_PATH.match(path)
            or path.rstrip("/") in ("/api/v1/tools/refresh", "/api/v1/imports/comfy-output")
            or path == "/openapi.json"
            or path.split("/")[1] in ("docs", "redoc")
        ):
            await reject("admin_only", "此操作仅在本机管理入口开放。")
            return
        if public:
            client = headers.get("cf-connecting-ip") or (scope.get("client") or ("unknown", 0))[0]
            try:
                client = str(ipaddress.ip_address(client))
            except ValueError:
                client = "unknown"
            minute = int(time.monotonic() // 60)
            if len(self.buckets) > 4096:
                self.buckets = {key: value for key, value in self.buckets.items() if key[2] == minute}
            key = (client, method not in SAFE_METHODS, minute)
            count = self.buckets.get(key, 0)
            limit = settings.public_writes_per_minute if method not in SAFE_METHODS else 600
            if count >= limit or len(self.buckets) > 8192:
                await reject("rate_limited", "操作过于频繁，请稍后再试。", 429, {"Retry-After": "60"})
                return
            self.buckets[key] = count + 1
        if method not in SAFE_METHODS:
            from starlette.requests import Request

            request = Request(scope)
            expected_session = self.state.public_session_token if public else self.state.session_token
            expected_csrf = self.state.public_csrf if public else self.state.csrf
            if (
                not origin
                or not secrets.compare_digest(
                    request.cookies.get("aiv_session", "").encode(), expected_session.encode()
                )
                or not secrets.compare_digest(headers.get("x-aiv-csrf", "").encode(), expected_csrf.encode())
            ):
                await reject("csrf_rejected", "页面会话已过期，请刷新重试。")
                return
        if public:
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                await reject("invalid_length", "文件大小信息无效。", 400)
                return
            if length < 0 or length > settings.public_max_request_bytes:
                await reject("upload_too_large", "共享工作台每次上传最多90MB，请分批上传。", 413)
                return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if public and received > settings.public_max_request_bytes:
                raise HTTPException(413, "共享工作台每次上传最多90MB，请分批上传。")
            return message

        async def response_send(message):
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["Referrer-Policy"] = "same-origin"
                response_headers["X-Request-ID"] = request_state["request_id"]
                response_headers["Content-Security-Policy"] = (
                    "frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
                )
                if path.startswith("/api/"):
                    response_headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, bounded_receive, response_send)
