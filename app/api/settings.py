from __future__ import annotations

import logging
import shutil

from fastapi import APIRouter, Depends, HTTPException

from ..config_store import ConfigError
from ..runtime import Ctx, get_ctx

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/disk")
async def get_disk(ctx: Ctx = Depends(get_ctx)) -> dict:
    probe = ctx.settings.download_root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法读取磁盘信息：{exc}") from exc
    watermark = float(ctx.config.get("low_watermark_gb") or 0)
    return {
        "path": str(ctx.settings.download_root),
        "probe": str(probe),
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "low_watermark_gb": watermark,
        "below_watermark": usage.free < watermark * 1024 ** 3,
    }


@router.get("")
async def get_settings(ctx: Ctx = Depends(get_ctx)) -> dict:
    return {
        "values": ctx.config.values,
        "download_root": str(ctx.settings.download_root),
        "config_dir": str(ctx.settings.config_dir),
    }


@router.put("")
async def update_settings(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    try:
        values = await ctx.config.update(payload or {})
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx.runner.apply_config()
    log.info("设置已更新：%s", ", ".join(f"{k}={v}" for k, v in (payload or {}).items()))
    return {"values": values}
