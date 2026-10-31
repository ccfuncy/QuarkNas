from __future__ import annotations

import base64
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import __version__, logbuf
from .api import account, events, files, logs, organize, settings as settings_api, tasks
from .config_store import ConfigStore
from .db import DB
from .download.queue import TaskRunner
from .events import EventBus
from .quark.auth import AccountStore
from .quark.qrlogin import QrLoginManager
from .runtime import Ctx
from .settings import load_settings

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logbuf.install(os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("quarknas")

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.ensure_dirs()

    db = DB(settings.db_path, settings.duckdb_path)
    await db.open()

    config = ConfigStore(db, settings)
    await config.load()

    bus = EventBus()
    account_store = AccountStore(db, settings)
    runner = TaskRunner(db, bus, account_store, settings, config)
    app.state.ctx = Ctx(
        settings=settings,
        config=config,
        db=db,
        bus=bus,
        account=account_store,
        runner=runner,
        qr=QrLoginManager(settings),
    )

    await runner.start()
    log.info(
        "QuarkNas 启动：下载目录 %s，并发 %d，分段 %d",
        settings.download_root,
        config.get("concurrency"),
        config.get("segments"),
    )
    try:
        yield
    finally:
        await runner.stop()
        await account_store.reset_client()
        await db.close()
        log.info("QuarkNas 已停止")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """单用户口令保护：未设置 AUTH_PASSWORD 时完全放行。"""

    def __init__(self, app, password: str) -> None:
        super().__init__(app)
        self.password = password

    async def dispatch(self, request: Request, call_next):
        if not self.password:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8", "ignore")
            except ValueError:
                raw = ""
            _, _, supplied = raw.partition(":")
            if hmac.compare_digest(supplied, self.password):
                return await call_next(request)
        return Response(
            "需要登录",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="QuarkNas"'},
        )


app = FastAPI(title="QuarkNas", version=__version__, lifespan=lifespan)

_settings_for_auth = load_settings()
app.add_middleware(BasicAuthMiddleware, password=_settings_for_auth.auth_password)

app.include_router(account.router)
app.include_router(files.router)
app.include_router(organize.router)
app.include_router(tasks.router)
app.include_router(events.router)
app.include_router(settings_api.router)
app.include_router(logs.router)


class FreshStaticFiles(StaticFiles):
    """静态资源强制回源校验。

    StaticFiles 默认不带 Cache-Control，浏览器会按启发式缓存长期复用旧
    app.js/app.css，功能更新后用户仍看到旧界面。加 no-cache 让浏览器
    每次用 ETag/Last-Modified 回源，未变化时仍返回 304。
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", FreshStaticFiles(directory=str(WEB_DIR / "static")), name="static")


@app.get("/", include_in_schema=False)
async def index(request: Request):
    ctx: Ctx = request.app.state.ctx
    return templates.TemplateResponse(
        request,
        "index.html",
        {"version": __version__, "download_root": str(ctx.settings.download_root)},
    )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"ok": True, "version": __version__}


def main() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
