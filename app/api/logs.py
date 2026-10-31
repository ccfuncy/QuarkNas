from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import __version__
from ..logbuf import LOG_BUFFER, LEVELS, redact
from ..runtime import Ctx, get_ctx

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs")
async def get_logs(level: str = "", q: str = "", limit: int = 200) -> dict:
    limit = max(1, min(1000, limit))
    normalized = level.upper() if level.upper() in LEVELS else ""
    return {"items": LOG_BUFFER.tail(normalized, q.strip(), limit)}


@router.get("/diagnostics")
async def diagnostics(ctx: Ctx = Depends(get_ctx)) -> JSONResponse:
    """导出脱敏诊断包：不含 Cookie、口令与文件内容。"""
    account = await ctx.account.status()
    tasks = await ctx.db.fetchall("SELECT * FROM task ORDER BY id DESC LIMIT 500")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": __version__,
        "settings": {
            "download_root": str(ctx.settings.download_root),
            "config_dir": str(ctx.settings.config_dir),
            "request_timeout": ctx.settings.request_timeout,
            "retries": ctx.settings.retries,
            "auth_enabled": bool(ctx.settings.auth_password),
        },
        "runtime": dict(ctx.config.values),
        "account": {
            "configured": account.get("configured"),
            "valid": account.get("valid"),
            "nickname": account.get("nickname"),
            "updated_at": account.get("updated_at"),
        },
        "tasks": [
            {
                "id": row["id"],
                "name": redact(row["name"] or ""),
                "src_path": redact(row["src_path"] or ""),
                "dst_path": redact(row["dst_path"] or ""),
                "size": row["size"],
                "downloaded": row["downloaded"],
                "status": row["status"],
                "error": redact(row["error"] or ""),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in tasks
        ],
        "logs": LOG_BUFFER.tail("", "", 500),
    }
    filename = f"quarknas-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
