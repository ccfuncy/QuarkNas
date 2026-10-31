from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ..quark.client import QuarkError
from ..runtime import Ctx, get_ctx
from .files import quark_http_error

log = logging.getLogger("quarknas.api.account")

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("")
async def account_status(ctx: Ctx = Depends(get_ctx)) -> dict:
    return await ctx.account.status()


@router.post("/check")
async def check_cookie(ctx: Ctx = Depends(get_ctx)) -> dict:
    return await ctx.account.check()


@router.get("/storage")
async def storage(ctx: Ctx = Depends(get_ctx)) -> dict:
    """账号容量：总容量与已用容量（字节）。"""
    try:
        client = await ctx.account.client()
        return await client.capacity()
    except QuarkError as exc:
        raise quark_http_error(exc) from exc


@router.post("/qr")
async def create_qr(ctx: Ctx = Depends(get_ctx)) -> dict:
    """生成扫码登录二维码，返回会话 id 与二维码图片地址。"""
    try:
        info = await ctx.qr.create()
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    info["image_url"] = f"/api/account/qr/{info['sid']}/image"
    return info


@router.get("/qr/{sid}/image")
async def qr_image(sid: str, ctx: Ctx = Depends(get_ctx)) -> Response:
    svg = ctx.qr.image(sid)
    if svg is None:
        raise HTTPException(status_code=404, detail="二维码不存在或已过期")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/qr/{sid}")
async def poll_qr(sid: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    """轮询扫码状态；确认后自动把 Cookie 落库。"""
    result = await ctx.qr.poll(sid)
    cookie = result.pop("cookie", "")
    if result.get("status") != "confirmed" or not cookie:
        return result
    try:
        status = await ctx.account.save(cookie)
    except ValueError as exc:
        log.exception("保存扫码 Cookie 失败（格式）")
        return {"status": "failed", "message": f"Cookie 保存失败：{exc}"}
    except QuarkError as exc:
        log.exception("保存扫码 Cookie 失败（校验）")
        return {"status": "failed", "message": f"Cookie 校验失败：{exc.message}"}
    except Exception as exc:  # noqa: BLE001
        log.exception("保存扫码 Cookie 失败")
        return {"status": "failed", "message": f"Cookie 保存异常：{exc}"}
    finally:
        await ctx.qr.close(sid)
    return {"status": "confirmed", "nickname": status.get("nickname") or result.get("nickname") or ""}
