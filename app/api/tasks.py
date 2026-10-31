from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..paths import resolve_target, safe_component
from ..quark.client import QuarkError
from ..runtime import Ctx, get_ctx
from .files import quark_http_error

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

MAX_ITEMS = 200
MAX_EXPANDED = 5000
ACTIVE = ("pending", "resolving", "downloading", "verifying")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Selection(BaseModel):
    fid: str
    name: str
    dir: bool = False
    size: int = 0
    path: str = ""


class CreateRequest(BaseModel):
    items: list[Selection] = Field(default_factory=list)
    keep_structure: bool = True


class BatchRequest(BaseModel):
    action: str
    ids: list[int] = Field(default_factory=list)
    delete_files: bool = False


class ReorderRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


BATCH_ACTIONS: dict[str, tuple[str, ...]] = {
    "pause": ("pending", "resolving", "downloading", "verifying"),
    "resume": ("paused",),
    "cancel": ("pending", "paused", "resolving", "downloading", "verifying"),
    "retry": ("failed", "canceled"),
    "delete": ("done", "failed", "canceled", "skipped"),
}


def _path_parts(path: str) -> list[str]:
    return [part for part in (path or "").split("/") if part.strip()]


def _segments(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _local_date(text: str | None):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).astimezone().date()
    except ValueError:
        return None


def _allocated_size(path: Path) -> int:
    """文件真正占用的字节数。

    Linux 上 .part 是 truncate 出来的稀疏文件，st_size 是完整大小而实际只占
    已写入的部分，直接拿 st_size 当"释放了多少"会严重虚高。
    """
    st = path.stat()
    blocks = getattr(st, "st_blocks", 0) or 0
    return blocks * 512 if blocks else st.st_size


def _purge_files(root: Path, dst_paths: list[str]) -> tuple[int, int]:
    """删除落盘文件（含未完成的 .part 临时文件），返回 (删除文件数, 释放字节数)。

    只处理下载根目录之内的路径；失败不影响任务记录的删除。
    """
    root_resolved = root.resolve()
    removed = 0
    freed = 0
    for raw in dst_paths:
        if not raw:
            continue
        try:
            target = Path(raw).resolve()
        except OSError:
            continue
        if target != root_resolved and root_resolved not in target.parents:
            log.warning("跳过下载目录之外的文件：%s", raw)
            continue
        for candidate in (target, Path(f"{target}.part"), Path(f"{target}.part.json")):
            try:
                if not candidate.is_file():
                    continue
                freed += _allocated_size(candidate)
                candidate.unlink()
                removed += 1
            except OSError as exc:
                log.warning("删除文件失败 %s：%s", candidate, exc)
        # 顺手收掉因删除而变空的文件夹，最多清理到下载根目录
        parent = target.parent
        while parent != root_resolved and root_resolved in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    if removed:
        log.info("已删除 %d 个文件，释放 %d 字节", removed, freed)
    return removed, freed


@router.get("/stats")
async def task_stats(ctx: Ctx = Depends(get_ctx)) -> dict:
    rows = await ctx.db.fetchall(
        "SELECT status, size, error, updated_at FROM task"
    )
    today = datetime.now().astimezone().date()
    buckets = {
        (today - timedelta(days=offset)).isoformat(): {
            "date": (today - timedelta(days=offset)).isoformat(),
            "count": 0,
            "bytes": 0,
        }
        for offset in range(6, -1, -1)
    }
    status_counts: dict[str, int] = {}
    failures: dict[str, int] = {}
    today_bytes = today_count = 0
    total_bytes = total_count = 0
    for row in rows:
        status = row["status"] or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "failed" and row["error"]:
            message = str(row["error"]).strip()
            failures[message] = failures.get(message, 0) + 1
        if status != "done":
            continue
        size = int(row["size"] or 0)
        total_bytes += size
        total_count += 1
        finished = _local_date(row["updated_at"])
        if finished == today:
            today_bytes += size
            today_count += 1
        bucket = buckets.get(finished.isoformat()) if finished else None
        if bucket is not None:
            bucket["count"] += 1
            bucket["bytes"] += size
    done_count = status_counts.get("done", 0)
    failed_count = status_counts.get("failed", 0)
    finished = done_count + failed_count
    return {
        "today": {"count": today_count, "bytes": today_bytes},
        "total": {"count": total_count, "bytes": total_bytes},
        "status_counts": status_counts,
        "success_rate": round(done_count / finished, 4) if finished else 0.0,
        "days": list(buckets.values()),
        "failures": [
            {"error": text, "count": count}
            for text, count in sorted(failures.items(), key=lambda item: -item[1])[:5]
        ],
    }


@router.get("/speed")
async def speed_history(seconds: int = 600, ctx: Ctx = Depends(get_ctx)) -> dict:
    """返回指定时间范围内的速度采样（毫秒时间戳 + B/s），供曲线回放。"""
    seconds = max(60, min(86400, seconds))
    since = int(
        (datetime.now(timezone.utc) - timedelta(seconds=seconds)).timestamp() * 1000
    )
    rows = await ctx.db.fetchall(
        "SELECT ts, speed FROM speed_sample WHERE ts >= ? ORDER BY ts", (since,)
    )
    return {"samples": [{"t": int(row["ts"]), "v": float(row["speed"])} for row in rows]}


@router.get("/export.csv")
async def export_tasks(ctx: Ctx = Depends(get_ctx)) -> Response:
    rows = await ctx.db.fetchall("SELECT * FROM task ORDER BY id DESC")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["id", "名称", "状态", "大小", "已下载", "重试次数", "批次", "失败原因",
         "源路径", "落盘路径", "创建时间", "更新时间"]
    )
    for row in rows:
        writer.writerow([
            row["id"], row["name"], row["status"], row["size"], row["downloaded"],
            row["retry_count"], row["batch_name"] or "", row["error"] or "",
            row["src_path"], row["dst_path"], row["created_at"], row["updated_at"],
        ])
    filename = f"quarknas-tasks-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("")
async def list_tasks(ctx: Ctx = Depends(get_ctx)) -> dict:
    rows = await ctx.db.fetchall("SELECT * FROM task ORDER BY id DESC LIMIT 200")
    tasks = [
        {
            "id": row["id"],
            "fid": row["fid"],
            "name": row["name"],
            "src_path": row["src_path"],
            "dst_path": row["dst_path"],
            "size": row["size"],
            "downloaded": row["downloaded"],
            "status": row["status"],
            "error": row["error"],
            "retry_count": row["retry_count"],
            "priority": row["priority"],
            "segments": _segments(row["segments_json"]),
            "batch_id": row["batch_id"],
            "batch_name": row["batch_name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return {
        "tasks": tasks,
        "counts": counts,
        "risk": ctx.runner.risk_state(),
        "download_root": str(ctx.settings.download_root),
    }


@router.post("")
async def create_tasks(req: CreateRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    if not req.items:
        raise HTTPException(status_code=400, detail="未选择任何文件")
    if len(req.items) > MAX_ITEMS:
        raise HTTPException(status_code=400, detail=f"一次最多选择 {MAX_ITEMS} 项")

    try:
        client = await ctx.account.client()
    except QuarkError as exc:
        raise quark_http_error(exc) from exc

    root = ctx.settings.download_root
    planned: list[tuple[str, str, str, int, str, str, str]] = []  # fid, name, src_path, size, dst_path, batch_id, batch_name

    try:
        for item in req.items:
            batch_id = uuid.uuid4().hex[:12]
            batch_name = item.name if item.dir else ""
            base = _path_parts(item.path) if req.keep_structure else []
            if item.dir:
                async for entry, rel in client.walk(item.fid):
                    parts = (base + ([item.name] if req.keep_structure else []) + rel + [entry["name"]])
                    dst = resolve_target(root, parts)
                    planned.append(
                        (
                            entry["fid"],
                            entry["name"],
                            "/" + "/".join(parts),
                            int(entry.get("size") or 0),
                            str(dst),
                            batch_id,
                            batch_name,
                        )
                    )
                    if len(planned) > MAX_EXPANDED:
                        raise HTTPException(
                            status_code=400, detail=f"展开后文件数超过 {MAX_EXPANDED}，请分批下载"
                        )
            else:
                parts = base + [item.name]
                dst = resolve_target(root, parts)
                planned.append(
                    (
                        item.fid,
                        item.name,
                        "/" + "/".join(parts),
                        int(item.size or 0),
                        str(dst),
                        batch_id,
                        batch_name,
                    )
                )
    except QuarkError as exc:
        raise quark_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not planned:
        raise HTTPException(status_code=400, detail="所选目录为空")

    # 同名同路径且仍在队列/下载中的任务不再重复创建。
    active_paths = {
        row["dst_path"]
        for row in await ctx.db.fetchall(
            "SELECT dst_path FROM task WHERE status IN ('pending','resolving','downloading','verifying','paused')"
        )
    }

    now = _now()
    ids: list[int] = []
    skipped = 0
    for fid, name, src_path, size, dst, batch_id, batch_name in planned:
        if dst in active_paths:
            skipped += 1
            continue
        task_id = await ctx.db.execute(
            """
            INSERT INTO task (fid, name, src_path, dst_path, size, downloaded, status,
                              batch_id, batch_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?)
            """,
            (fid, safe_component(name), src_path, dst, size, batch_id, batch_name, now, now),
        )
        ids.append(task_id)
        active_paths.add(dst)

    for task_id in ids:
        await ctx.runner.enqueue(task_id)
        await ctx.runner.publish(task_id)

    log.info("创建下载任务 %d 个（跳过重复 %d 个）", len(ids), skipped)
    return {"created": len(ids), "skipped": skipped, "ids": ids}


@router.post("/reorder")
async def reorder_tasks(req: ReorderRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    ids = list(dict.fromkeys(req.ids))
    if not ids:
        raise HTTPException(status_code=400, detail="排序列表为空")
    if len(ids) > 1000:
        raise HTTPException(status_code=400, detail="一次最多排序 1000 个任务")
    marks = ",".join("?" for _ in ids)
    rows = await ctx.db.fetchall(
        f"SELECT id, status FROM task WHERE id IN ({marks})", ids
    )
    pending = {row["id"] for row in rows if row["status"] == "pending"}
    ordered = [task_id for task_id in ids if task_id in pending]
    if not ordered:
        raise HTTPException(status_code=400, detail="没有可排序的排队任务")
    total = len(ordered)
    now = _now()
    for index, task_id in enumerate(ordered):
        await ctx.db.execute(
            "UPDATE task SET priority = ?, updated_at = ? WHERE id = ?",
            (total - index, now, task_id),
        )
    count = await ctx.runner.reorder(ordered)
    return {"ok": True, "count": count}


@router.post("/batch")
async def batch_action(req: BatchRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    statuses = BATCH_ACTIONS.get(req.action)
    if statuses is None:
        raise HTTPException(status_code=400, detail="不支持的操作")
    if len(req.ids) > 500:
        raise HTTPException(status_code=400, detail="一次最多操作 500 个任务")

    if req.ids:
        marks = ",".join("?" for _ in req.ids)
        rows = await ctx.db.fetchall(
            f"SELECT id, status, dst_path FROM task WHERE id IN ({marks})", req.ids
        )
    else:
        marks = ",".join("?" for _ in statuses)
        rows = await ctx.db.fetchall(
            f"SELECT id, status, dst_path FROM task WHERE status IN ({marks})", statuses
        )

    files_removed = 0
    freed = 0
    if req.action == "delete" and req.delete_files:
        targets = [row["dst_path"] for row in rows if row["status"] in statuses]
        files_removed, freed = await asyncio.to_thread(
            _purge_files, ctx.settings.download_root, targets
        )

    ok = 0
    failed = 0
    for row in rows:
        task_id = row["id"]
        if row["status"] not in statuses:
            continue
        if req.action == "delete":
            await ctx.db.execute("DELETE FROM task WHERE id = ?", (task_id,))
            ctx.bus.publish({"type": "removed", "data": {"id": task_id}})
            ok += 1
            continue
        result = await getattr(ctx.runner, req.action)(task_id)
        if result.get("ok"):
            ok += 1
        else:
            failed += 1

    log.info("批量操作 %s：成功 %d 个，失败 %d 个", req.action, ok, failed)
    return {
        "ok": ok,
        "failed": failed,
        "total": len(rows),
        "files_removed": files_removed,
        "freed": freed,
    }


@router.post("/{task_id}/retry")
async def retry_task(task_id: int, ctx: Ctx = Depends(get_ctx)) -> dict:
    result = await ctx.runner.retry(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "无法重试"))
    return result


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int, ctx: Ctx = Depends(get_ctx)) -> dict:
    result = await ctx.runner.cancel(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "无法取消"))
    return result


@router.post("/{task_id}/pause")
async def pause_task(task_id: int, ctx: Ctx = Depends(get_ctx)) -> dict:
    result = await ctx.runner.pause(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "无法暂停"))
    return result


@router.post("/{task_id}/resume")
async def resume_task(task_id: int, ctx: Ctx = Depends(get_ctx)) -> dict:
    result = await ctx.runner.resume(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "无法恢复"))
    return result


@router.post("/{task_id}/top")
async def top_task(task_id: int, ctx: Ctx = Depends(get_ctx)) -> dict:
    result = await ctx.runner.top(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "无法置顶"))
    return result


@router.delete("/{task_id}")
async def delete_task(
    task_id: int, delete_files: bool = False, ctx: Ctx = Depends(get_ctx)
) -> dict:
    row = await ctx.db.fetchone(
        "SELECT status, dst_path FROM task WHERE id = ?", (task_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] in ACTIVE:
        raise HTTPException(status_code=400, detail="任务进行中，请先取消")
    files_removed = 0
    freed = 0
    if delete_files:
        files_removed, freed = await asyncio.to_thread(
            _purge_files, ctx.settings.download_root, [row["dst_path"]]
        )
    await ctx.db.execute("DELETE FROM task WHERE id = ?", (task_id,))
    ctx.bus.publish({"type": "removed", "data": {"id": task_id}})
    return {"ok": True, "files_removed": files_removed, "freed": freed}


@router.post("/clear-finished")
async def clear_finished(
    delete_files: bool = False, ctx: Ctx = Depends(get_ctx)
) -> dict:
    rows = await ctx.db.fetchall(
        "SELECT id, dst_path FROM task WHERE status IN ('done','failed','canceled','skipped')"
    )
    files_removed = 0
    freed = 0
    if delete_files:
        files_removed, freed = await asyncio.to_thread(
            _purge_files, ctx.settings.download_root, [row["dst_path"] for row in rows]
        )
    await ctx.db.execute(
        "DELETE FROM task WHERE status IN ('done','failed','canceled','skipped')"
    )
    for row in rows:
        ctx.bus.publish({"type": "removed", "data": {"id": row["id"]}})
    return {
        "removed": len(rows),
        "files_removed": files_removed,
        "freed": freed,
    }
