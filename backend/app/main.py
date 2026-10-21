import asyncio
import contextlib
import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager, closing
from logging.handlers import RotatingFileHandler

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .api import router
from .comfy import ComfyClient
from .config import ROOT, load_settings
from .conversation_api import router as conversation_router
from .db import Database
from .errors import AppError
from .extensions import router as extension_router
from .maintenance import MaintenanceService
from .models import Setting
from .planner import Planner
from .plans import PlanService
from .registry import Registry
from .storage import Storage
from .worker import Worker


def migrate(settings):
    path = settings.data_dir / "app.sqlite3"
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    # Migration environment uses the same configured data directory.
    from alembic.script import ScriptDirectory

    if path.exists():
        with closing(sqlite3.connect(path)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='alembic_version'").fetchone()
            version = db.execute("SELECT version_num FROM alembic_version").fetchone() if exists else None
            head = ScriptDirectory.from_config(config).get_current_head()
            if not version or version[0] != head:
                with closing(
                    sqlite3.connect(
                        settings.data_dir / "backups" / ("before-migration-" + secrets.token_hex(6) + ".sqlite3")
                    )
                ) as backup:
                    db.backup(backup)
    command.upgrade(config, "head")


@asynccontextmanager
async def lifespan(app):
    settings = load_settings()
    if settings.host not in ("127.0.0.1", "localhost"):
        raise RuntimeError("首版只能绑定本机回环地址。")
    lock = (settings.data_dir / "app.lock").open("a+b")
    import msvcrt

    lock.seek(0)
    if lock.read(1) == b"":
        lock.write(b"1")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        lock.close()
        raise RuntimeError("当前素材目录已有一个映作服务运行。") from exc
    handler = RotatingFileHandler(
        settings.data_dir / "logs" / "app.log", maxBytes=2 * 1024**2, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger("aiv").addHandler(handler)
    logging.getLogger("aiv").setLevel(logging.INFO)
    await asyncio.to_thread(migrate, settings)
    db = Database(settings.data_dir / "app.sqlite3")
    async with db.session() as s:
        stored = await s.get(Setting, "runtime")
        if stored:
            for k, v in stored.value.items():
                if k != "revision":
                    setattr(settings, k, v)
    state = app.state
    state.settings, state.db = settings, db
    state.session_token, state.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    state.registry = Registry(ROOT / "tool_registry", settings.data_dir)
    state.storage = Storage(db, settings)
    state.comfy = ComfyClient(settings.comfy_url, settings.comfy_output_root)
    state.plans = PlanService(db, state.registry, state.storage, settings)
    state.planner = Planner(db, settings, state.registry, state.plans, state.storage)
    state.worker = Worker(db, state.comfy, state.registry, state.storage, settings)
    state.maintenance = MaintenanceService(db, settings, state.storage)
    await state.maintenance.recover()
    worker = asyncio.create_task(state.worker.run())
    try:
        yield
    finally:
        await state.worker.stop()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await state.comfy.close()
        await db.close()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()
        logging.getLogger("aiv").removeHandler(handler)
        handler.close()


app = FastAPI(title="映作 · AIV Studio", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(conversation_router)
app.include_router(extension_router)


@app.middleware("http")
async def local_security(request: Request, call_next):
    request.state.request_id = secrets.token_hex(8)
    host = request.headers.get("host", "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost"):
        return JSONResponse({"error": {"code": "host_rejected", "message": "只接受本机请求。"}}, status_code=403)
    allowed = {f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (app.state.settings.port, 5173)}
    origin = request.headers.get("origin")
    if origin and origin not in allowed:
        return JSONResponse({"error": {"code": "origin_rejected", "message": "请求来源不受信任。"}}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if (
            not origin
            or request.cookies.get("aiv_session") != app.state.session_token
            or request.headers.get("x-aiv-csrf") != app.state.csrf
        ):
            return JSONResponse(
                {"error": {"code": "csrf_rejected", "message": "页面会话已过期，请刷新重试。"}}, status_code=403
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(AppError)
async def app_error(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "retryable": exc.retryable,
                "request_id": request.state.request_id,
            }
        },
        status_code=exc.status,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": "request_invalid",
                "message": "请求参数不符合要求。",
                "details": {
                    "errors": [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
                },
            }
        },
        status_code=422,
    )


@app.exception_handler(Exception)
async def internal_error(request, exc):
    logging.getLogger("aiv.api").exception("request failed")
    return JSONResponse(
        {"error": {"code": "internal_error", "message": "应用处理失败，请查看设置中的诊断信息。"}}, status_code=500
    )


@app.get("/{path:path}", include_in_schema=False)
async def frontend(path: str):
    root = ROOT / "frontend" / "dist"
    if path.startswith("api/"):
        raise AppError("not_found", "接口不存在。", 404)
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise AppError("invalid_path", "路径不可访问。", 404)
    if not target.is_file():
        target = root / "index.html"
    if not target.exists():
        raise AppError("frontend_not_built", "请先运行scripts/setup.ps1构建界面。", 503)
    return FileResponse(target)
