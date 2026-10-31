from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..quark.client import DEFAULT_SORT, CookieExpired, QuarkError
from ..runtime import Ctx, get_ctx

router = APIRouter(prefix="/api", tags=["files"])

# 目录大小需要逐层遍历云端目录，限制单次请求的目录数与总并发，避免触发风控。
MAX_SIZE_DIRS = 12
SIZE_CONCURRENCY = 4
SIZE_ITEM_BUDGET = 8000

# 前端只允许按这几列排序，字段名翻译成夸克 _sort 里的字段，避免把用户输入拼进查询串。
SORT_FIELDS = {"name": "file_name", "size": "size", "time": "updated_at"}


def _build_sort(field: str, order: str) -> str:
    key = SORT_FIELDS.get((field or "").strip())
    if not key:
        return DEFAULT_SORT
    direction = "asc" if (order or "").strip().lower() == "asc" else "desc"
    # file_type 固定在最前，保证文件夹永远排在文件前面。
    return f"file_type:asc,{key}:{direction}"


def quark_http_error(exc: QuarkError) -> HTTPException:
    return HTTPException(
        status_code=401 if isinstance(exc, CookieExpired) else 502,
        detail=exc.message,
    )


@router.get("/files")
async def list_files(
    fid: str = "0",
    page: int = 1,
    size: int = 50,
    sort: str = "",
    order: str = "",
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    page = max(1, page)
    size = max(1, min(200, size))
    try:
        client = await ctx.account.client()
        result = await client.list_files(
            pdir_fid=fid, page=page, size=size, sort=_build_sort(sort, order)
        )
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {
        "fid": fid,
        "page": result["page"],
        "size": result["size"],
        "total": result["total"],
        "entries": result["entries"],
    }


@router.get("/files/sizes")
async def dir_sizes(fids: str = "", ctx: Ctx = Depends(get_ctx)) -> dict:
    """批量统计目录大小。size 为 null 表示统计失败，complete=false 表示超出预算被截断。"""
    ids = list(dict.fromkeys(part.strip() for part in fids.split(",") if part.strip()))
    if not ids:
        return {"sizes": {}}
    if len(ids) > MAX_SIZE_DIRS:
        raise HTTPException(status_code=400, detail=f"一次最多查询 {MAX_SIZE_DIRS} 个目录")
    try:
        client = await ctx.account.client()
    except QuarkError as exc:
        raise quark_http_error(exc) from exc

    semaphore = asyncio.Semaphore(SIZE_CONCURRENCY)
    budget = {"left": SIZE_ITEM_BUDGET}
    results = await asyncio.gather(
        *(client.dir_size(fid, semaphore=semaphore, budget=budget) for fid in ids),
        return_exceptions=True,
    )
    sizes: dict[str, dict] = {}
    for fid, result in zip(ids, results):
        if isinstance(result, BaseException):
            sizes[fid] = {"size": None, "complete": False}
        else:
            total, complete = result
            sizes[fid] = {"size": total, "complete": complete}
    return {"sizes": sizes}


@router.get("/files/search")
async def search_files(
    keyword: str,
    page: int = 1,
    size: int = 50,
    sort: str = "",
    order: str = "",
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    keyword = (keyword or "").strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="请输入搜索关键词")
    page = max(1, page)
    size = max(1, min(200, size))
    try:
        client = await ctx.account.client()
        result = await client.search_files(
            keyword, page=page, size=size, sort=_build_sort(sort, order)
        )
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {
        "keyword": keyword,
        "page": result["page"],
        "size": result["size"],
        "total": result["total"],
        "entries": result["entries"],
    }


class RenameRequest(BaseModel):
    fid: str
    name: str


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="名称不能包含 / 或 \\")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="名称过长（最多 255 个字符）")
    return name


def _check_fids(fids: list[str]) -> list[str]:
    ids = list(dict.fromkeys((fid or "").strip() for fid in fids if (fid or "").strip()))
    if not ids:
        raise HTTPException(status_code=400, detail="缺少文件 id")
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="一次最多操作 100 项")
    return ids


@router.post("/files/rename")
async def rename_entry(req: RenameRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    fid = (req.fid or "").strip()
    if not fid:
        raise HTTPException(status_code=400, detail="缺少文件 id")
    name = _check_name(req.name)
    try:
        client = await ctx.account.client()
        await client.rename(fid, name)
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {"ok": True, "fid": fid, "name": name}


class MoveRequest(BaseModel):
    fids: list[str]
    to_fid: str = ""


@router.post("/files/move")
async def move_entries(req: MoveRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    fids = _check_fids(req.fids)
    to_fid = (req.to_fid or "").strip() or "0"
    if to_fid in fids:
        raise HTTPException(status_code=400, detail="不能把文件夹移动到它自己里面")
    try:
        client = await ctx.account.client()
        await client.move(fids, to_fid)
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {"ok": True, "fids": fids, "to_fid": to_fid}


class DeleteRequest(BaseModel):
    fids: list[str]


class RecordRequest(BaseModel):
    """回收站操作用 record_id，不是普通文件 fid。"""

    records: list[str]


@router.post("/files/delete")
async def delete_entries(req: DeleteRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    fids = _check_fids(req.fids)
    try:
        client = await ctx.account.client()
        await client.delete(fids)
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {"ok": True, "fids": fids}


class FolderRequest(BaseModel):
    pdir_fid: str = "0"
    name: str


@router.get("/files/recycle")
async def list_recycle(
    page: int = 1,
    size: int = 50,
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    page = max(1, page)
    size = max(1, min(200, size))
    try:
        client = await ctx.account.client()
        result = await client.list_recycle(page=page, size=size)
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {
        "page": result["page"],
        "size": result["size"],
        "total": result["total"],
        "entries": result["entries"],
    }


@router.post("/files/recycle/restore")
async def restore_recycle(req: RecordRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    records = _check_fids(req.records)
    try:
        client = await ctx.account.client()
        await client.recover(records)
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {"ok": True, "records": records}


@router.post("/files/recycle/purge")
async def purge_recycle(req: RecordRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    records = _check_fids(req.records)
    try:
        client = await ctx.account.client()
        await client.purge(records)
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {"ok": True, "records": records}


@router.post("/files/recycle/clear")
async def clear_recycle(ctx: Ctx = Depends(get_ctx)) -> dict:
    try:
        client = await ctx.account.client()
        await client.clear_recycle()
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {"ok": True}


@router.post("/files/folder")
async def create_folder(req: FolderRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    pdir_fid = (req.pdir_fid or "").strip() or "0"
    name = _check_name(req.name)
    try:
        client = await ctx.account.client()
        fid = await client.create_folder(pdir_fid, name)
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    return {"ok": True, "fid": fid, "name": name}
