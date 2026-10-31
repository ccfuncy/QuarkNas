from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import math
import shutil
import time
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from ..config_store import ConfigStore
from ..db import DB
from ..events import EventBus
from ..quark.auth import AccountStore
from ..quark.client import CookieExpired, QuarkError
from ..settings import Settings
from .engine import Cancelled, DownloadError, RateLimiter, RiskControlError, download_to

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "resolving", "downloading", "verifying")
BUSY_STATUSES = ACTIVE_STATUSES + ("paused",)

# 风控自适应：统计窗口内连续出现 403 次即降并发并冷却。
RISK_WINDOW = 180.0
RISK_THRESHOLD = 2
RISK_BASE_COOLDOWN = 120.0
RISK_MAX_COOLDOWN = 600.0
RISK_MAX_SEGMENTS = 2
RISK_FIRST_DELAY = 60.0

# 速度采样：每 2 秒汇总一次正在下载的任务速度写入数据库，刷新页面后曲线不丢。
SPEED_SAMPLE_INTERVAL = 2.0
SPEED_STALE = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_clock(text: str) -> clock_time | None:
    try:
        hour, minute = str(text).split(":")
        return clock_time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def _in_window(now: clock_time, start: clock_time, end: clock_time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _seconds_until_open(now: datetime, start: clock_time) -> float:
    target = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class TaskRunner:
    def __init__(
        self,
        db: DB,
        bus: EventBus,
        account: AccountStore,
        settings: Settings,
        config: ConfigStore,
    ) -> None:
        self.db = db
        self.bus = bus
        self.account = account
        self.settings = settings
        self.config = config
        self._queue: asyncio.PriorityQueue[tuple[int, int, int, int, int]] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._token_seq = itertools.count(1)
        self._tokens: dict[int, int] = {}
        self._order = str(config.get("queue_order") or "time")
        self._workers: dict[int, asyncio.Task] = {}
        self._active: dict[int, int] = {}
        self._stop_events: dict[int, asyncio.Event] = {}
        self._queued: set[int] = set()
        self._paused: set[int] = set()
        self._deferred: dict[int, asyncio.Task] = {}
        self._desired_workers = max(1, int(config.get("concurrency")))
        self._zombies: set[asyncio.Task] = set()
        self._janitor: asyncio.Task | None = None
        self._speed_sampler: asyncio.Task | None = None
        # task_id -> (monotonic 时间, 已下载字节, 平滑速度)
        self._speed_state: dict[int, tuple[float, int, float]] = {}
        self._limiter = RateLimiter(self._rate_limit_bytes())
        self._risk_hits: list[float] = []
        self._risk_strikes = 0
        self._risk_until = 0.0
        self._risk_workers = self._desired_workers

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        await self._recover()
        self.apply_config()
        self._janitor = asyncio.create_task(self._janitor_loop(), name="dl-cleanup")
        self._speed_sampler = asyncio.create_task(self._speed_loop(), name="dl-speed")
        log.info("下载队列已启动：并发 %d，分段 %d", self._desired_workers, self.config.get("segments"))

    async def stop(self) -> None:
        if self._janitor is not None:
            self._janitor.cancel()
            await asyncio.gather(self._janitor, return_exceptions=True)
            self._janitor = None
        if self._speed_sampler is not None:
            self._speed_sampler.cancel()
            await asyncio.gather(self._speed_sampler, return_exceptions=True)
            self._speed_sampler = None
        self._speed_state.clear()
        for timer in self._deferred.values():
            timer.cancel()
        self._deferred.clear()
        for event in self._stop_events.values():
            event.set()
        for worker in list(self._workers.values()):
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        if self._zombies:
            await asyncio.gather(*self._zombies, return_exceptions=True)
            self._zombies.clear()

    def apply_config(self) -> None:
        """配置变更后即时生效：调整 worker 数量与全局限速。"""
        self._limiter.set_rate(self._rate_limit_bytes())
        order = str(self.config.get("queue_order") or "time")
        if order != self._order:
            self._order = order
            try:
                asyncio.get_running_loop().create_task(self._requeue_pending())
            except RuntimeError:
                pass
        desired = max(1, int(self.config.get("concurrency")))
        self._desired_workers = desired
        self._risk_workers = self._risk_workers_for(desired)
        for index, worker in list(self._workers.items()):
            if worker.done():
                self._workers.pop(index, None)
        for index in range(desired):
            if index not in self._workers:
                self._workers[index] = asyncio.create_task(
                    self._worker(index), name=f"dl-worker-{index}"
                )
        # 缩容时先取消空闲 worker，忙的等当前任务结束后自行退出。
        for index, worker in list(self._workers.items()):
            if index >= desired and index not in self._active:
                worker.cancel()
                self._workers.pop(index, None)
                self._zombies.add(worker)
                worker.add_done_callback(self._zombies.discard)

    def _rate_limit_bytes(self) -> float:
        try:
            mbps = float(self.config.get("speed_limit_mbps") or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, mbps) * 1024 * 1024

    # ------------------------------------------------------------------ 风控自适应

    def _risk_active(self) -> bool:
        return self._risk_until > time.monotonic()

    def _risk_workers_for(self, desired: int) -> int:
        return max(1, desired // 2) if self._risk_active() else desired

    def risk_state(self) -> dict:
        now = time.monotonic()
        active = now < self._risk_until
        return {
            "active": active,
            "seconds": int(math.ceil(self._risk_until - now)) if active else 0,
            "concurrency": self._risk_workers if active else self._desired_workers,
            "segments": self._segments_now(),
            "strikes": self._risk_strikes,
        }

    def _segments_now(self) -> int:
        try:
            base = int(self.config.get("segments") or 4)
        except (TypeError, ValueError):
            base = 4
        if self._risk_active():
            return max(1, min(RISK_MAX_SEGMENTS, base))
        return max(1, base)

    async def _publish_risk(self) -> None:
        self.bus.publish({"type": "risk", "data": self.risk_state()})

    async def _note_risk(self) -> float:
        """记录一次 403，返回建议的冷却秒数；未达到阈值返回 0。"""
        now = time.monotonic()
        self._risk_hits = [hit for hit in self._risk_hits if now - hit <= RISK_WINDOW]
        self._risk_hits.append(now)
        if len(self._risk_hits) < RISK_THRESHOLD:
            return 0.0
        self._risk_hits.clear()
        self._risk_strikes = min(self._risk_strikes + 1, 6)
        cooldown = min(
            RISK_MAX_COOLDOWN, RISK_BASE_COOLDOWN * 2 ** (self._risk_strikes - 1)
        )
        self._risk_until = max(self._risk_until, now) + cooldown
        self._risk_workers = max(1, self._desired_workers // 2)
        await self._publish_risk()
        log.warning(
            "连续 403：进入风控冷却 %d 秒，并发降至 %d，分段降至 %d",
            int(cooldown),
            self._risk_workers,
            self._segments_now(),
        )
        return cooldown

    async def _expire_risk(self) -> None:
        if self._risk_until and not self._risk_active():
            self._risk_until = 0.0
            self._risk_workers = self._desired_workers
            await self._publish_risk()
            log.info("风控冷却结束，恢复并发 %d", self._desired_workers)

    async def _clear_risk(self) -> None:
        """有任务成功完成后清空风控计数，恢复正常并发。"""
        if not (self._risk_strikes or self._risk_until or self._risk_hits):
            return
        self._risk_hits.clear()
        self._risk_strikes = 0
        self._risk_until = 0.0
        self._risk_workers = self._desired_workers
        await self._publish_risk()
        log.info("下载成功，风控计数已清零")

    async def _risk_gate(self, index: int) -> None:
        """冷却期内多余的 worker 在取任务前等待，避免继续加压。"""
        while True:
            if not self._risk_active():
                if self._risk_until:
                    await self._expire_risk()
                return
            if index < self._risk_workers:
                return
            wait = min(5.0, max(0.2, self._risk_until - time.monotonic()))
            await asyncio.sleep(wait)

    async def _recover(self) -> None:
        rows = await self.db.fetchall(
            "SELECT id FROM task WHERE status IN ('resolving', 'downloading', 'verifying')"
        )
        for row in rows:
            await self.db.execute(
                "UPDATE task SET status = 'pending', updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
        pending = await self.db.fetchall("SELECT id FROM task WHERE status = 'pending' ORDER BY id")
        for row in pending:
            await self.enqueue(row["id"])
        if rows:
            log.info("恢复 %d 个中断任务", len(rows))

    async def _janitor_loop(self) -> None:
        """每小时检查一次，清理超过保留期的已结束任务记录。"""
        while True:
            try:
                await self.cleanup_finished()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("自动清理任务记录失败", exc_info=True)
            await asyncio.sleep(3600)

    async def cleanup_finished(self) -> int:
        """删除 updated_at 早于保留期的已完成/失败/取消任务记录，只删记录不删文件。"""
        try:
            days = int(self.config.get("cleanup_days") or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        rows = await self.db.fetchall(
            "SELECT id FROM task WHERE status IN ('done', 'failed', 'canceled', 'skipped')"
            " AND updated_at < ?",
            (cutoff,),
        )
        for row in rows:
            await self.db.execute("DELETE FROM task WHERE id = ?", (row["id"],))
            self.bus.publish({"type": "removed", "data": {"id": row["id"]}})
        if rows:
            log.info("自动清理 %d 条任务记录（保留 %d 天）", len(rows), days)
        return len(rows)

    # ------------------------------------------------------------------ 速度采样

    def _note_speed(self, task_id: int, downloaded: int) -> None:
        """用相邻两次进度回调的字节差计算瞬时速度，并做指数平滑。"""
        now = time.monotonic()
        previous = self._speed_state.get(task_id)
        if previous is None:
            self._speed_state[task_id] = (now, downloaded, 0.0)
            return
        last_at, last_bytes, last_speed = previous
        elapsed = now - last_at
        if elapsed <= 0:
            return
        delta = downloaded - last_bytes
        if delta < 0:
            self._speed_state[task_id] = (now, downloaded, 0.0)
            return
        instant = delta / elapsed
        speed = last_speed * 0.65 + instant * 0.35 if last_speed > 0 else instant
        self._speed_state[task_id] = (now, downloaded, speed)

    async def _speed_loop(self) -> None:
        """定期把当前总速度写入数据库，页面刷新/重启后仍能画出历史曲线。"""
        while True:
            try:
                await asyncio.sleep(SPEED_SAMPLE_INTERVAL)
                now = time.monotonic()
                total = 0.0
                active = False
                for seen_at, _bytes, speed in list(self._speed_state.values()):
                    if now - seen_at > SPEED_STALE:
                        continue
                    active = True
                    total += max(0.0, speed)
                if not active:
                    continue
                await self.db.execute(
                    "INSERT OR IGNORE INTO speed_sample (ts, speed) VALUES (?, ?)",
                    (int(time.time() * 1000), round(total, 2)),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 采样失败不影响下载
                log.warning("记录下载速度失败", exc_info=True)

    # ------------------------------------------------------------------ 任务队列

    async def enqueue(self, task_id: int, force: bool = False) -> None:
        """入队：手动置顶优先，同级按 queue_order（时间/大小）排序。"""
        if task_id in self._queued and not force:
            return
        self._queued.add(task_id)
        token = next(self._token_seq)
        self._tokens[task_id] = token
        row = await self.db.fetchone("SELECT priority, size FROM task WHERE id = ?", (task_id,))
        manual = int(row["priority"] or 0) if row else 0
        size = int(row["size"] or 0) if row else 0
        if self._order == "size_desc":
            rank = -size
        elif self._order == "size_asc":
            rank = size
        else:
            rank = 0
        self._queue.put_nowait((-manual, rank, next(self._seq), task_id, token))

    async def _requeue_pending(self) -> None:
        """队列顺序设置变化后，按新顺序重排所有排队任务。"""
        rows = await self.db.fetchall("SELECT id FROM task WHERE status = 'pending' ORDER BY id")
        for row in rows:
            await self.enqueue(row["id"], force=True)

    async def top(self, task_id: int) -> dict:
        row = await self.db.fetchone("SELECT status FROM task WHERE id = ?", (task_id,))
        if not row:
            return {"ok": False, "message": "任务不存在"}
        if row["status"] != "pending":
            return {"ok": False, "message": "只有排队中的任务可以置顶"}
        current = await self.db.fetchone("SELECT COALESCE(MAX(priority), 0) AS value FROM task")
        priority = int(current["value"] or 0) + 1
        await self.db.execute(
            "UPDATE task SET priority = ?, updated_at = ? WHERE id = ?",
            (priority, _now(), task_id),
        )
        await self.enqueue(task_id, force=True)
        await self.publish(task_id)
        log.info("任务 %s 已置顶（优先级 %d）", task_id, priority)
        return {"ok": True, "priority": priority}

    async def reorder(self, ids: list[int]) -> int:
        """按给定顺序重排排队任务（前端拖拽排序）。"""
        for task_id in ids:
            await self.enqueue(task_id, force=True)
            await self.publish(task_id)
        log.info("排队任务已重排：%s", " > ".join(str(i) for i in ids[:10]))
        return len(ids)

    async def _set_status(self, task_id: int, status: str, **fields: Any) -> None:
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, _now()]
        for key, value in fields.items():
            updates.append(f"{key} = ?")
            params.append(value)
        params.append(task_id)
        await self.db.execute(f"UPDATE task SET {', '.join(updates)} WHERE id = ?", params)
        await self.publish(task_id)

    async def snapshot(self, task_id: int) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM task WHERE id = ?", (task_id,))
        if not row:
            return None
        segments: list[dict] = []
        if row["segments_json"]:
            try:
                segments = json.loads(row["segments_json"])
            except ValueError:
                segments = []
        return {
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
            "segments": segments,
            "batch_id": row["batch_id"],
            "batch_name": row["batch_name"],
            "updated_at": row["updated_at"],
        }

    async def publish(self, task_id: int) -> None:
        data = await self.snapshot(task_id)
        if data:
            self.bus.publish({"type": "task", "data": data})

    async def pause(self, task_id: int) -> dict:
        row = await self.db.fetchone("SELECT status FROM task WHERE id = ?", (task_id,))
        if not row:
            return {"ok": False, "message": "任务不存在"}
        status = row["status"]
        if status == "paused":
            return {"ok": True}
        if status == "pending":
            self._cancel_deferred(task_id)
            self._paused.add(task_id)
            await self._set_status(task_id, "paused", error=None)
            await self._webhook(task_id, "paused")
            log.info("任务 %s 已暂停（排队中）", task_id)
            return {"ok": True}
        if status in ("resolving", "downloading", "verifying"):
            self._paused.add(task_id)
            event = self._stop_events.get(task_id)
            if event is not None:
                event.set()
            log.info("任务 %s 正在暂停", task_id)
            return {"ok": True}
        return {"ok": False, "message": "当前状态不可暂停"}

    async def resume(self, task_id: int) -> dict:
        row = await self.db.fetchone("SELECT status FROM task WHERE id = ?", (task_id,))
        if not row:
            return {"ok": False, "message": "任务不存在"}
        if row["status"] != "paused":
            return {"ok": False, "message": "任务未暂停"}
        self._paused.discard(task_id)
        await self._set_status(task_id, "pending", error=None)
        await self.enqueue(task_id)
        log.info("任务 %s 已恢复", task_id)
        return {"ok": True}

    async def cancel(self, task_id: int) -> dict:
        row = await self.db.fetchone("SELECT status FROM task WHERE id = ?", (task_id,))
        if not row:
            return {"ok": False, "message": "任务不存在"}
        status = row["status"]
        if status in ("resolving", "downloading", "verifying"):
            event = self._stop_events.get(task_id)
            if event is not None:
                event.set()
            return {"ok": True}
        if status in ("pending", "paused"):
            self._paused.discard(task_id)
            self._cancel_deferred(task_id)
            await self._set_status(task_id, "canceled", error="已取消")
            return {"ok": True}
        return {"ok": False, "message": "当前状态无需取消"}

    # ------------------------------------------------------------------ 下载时段

    def _cancel_deferred(self, task_id: int) -> None:
        timer = self._deferred.pop(task_id, None)
        if timer is not None:
            timer.cancel()

    def _defer(self, task_id: int, seconds: float) -> None:
        self._cancel_deferred(task_id)

        async def _later() -> None:
            try:
                await asyncio.sleep(min(max(seconds, 1.0), 3600.0))
            except asyncio.CancelledError:
                return
            self._deferred.pop(task_id, None)
            await self.enqueue(task_id)

        self._deferred[task_id] = asyncio.create_task(_later(), name=f"dl-defer-{task_id}")

    async def _wait_schedule(self, task_id: int) -> bool:
        if not self.config.get("schedule_enabled"):
            return True
        start = _parse_clock(self.config.get("schedule_start"))
        end = _parse_clock(self.config.get("schedule_end"))
        if start is None or end is None:
            return True
        now = datetime.now()
        if _in_window(now.time(), start, end):
            return True
        seconds = _seconds_until_open(now, start)
        self._defer(task_id, seconds)
        log.info("任务 %s 在下载时段外，将在 %d 分钟后重试", task_id, max(1, int(seconds // 60)))
        return False

    # ------------------------------------------------------------------ 执行

    async def _worker(self, index: int) -> None:
        while True:
            if index >= self._desired_workers:
                self._workers.pop(index, None)
                return
            await self._risk_gate(index)
            item = await self._queue.get()
            task_id, token = item[3], item[4]
            if self._tokens.get(task_id) != token:
                # 旧条目（已置顶/重排队）直接跳过。
                self._queue.task_done()
                continue
            self._tokens.pop(task_id, None)
            self._queued.discard(task_id)
            self._active[index] = task_id
            try:
                await self._process(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 兜底，避免 worker 退出
                log.exception("任务 %s 处理异常", task_id)
                try:
                    await self._set_status(task_id, "failed", error="内部错误，请查看日志")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._active.pop(index, None)
                self._queue.task_done()

    def _free_space(self, directory: Path) -> int:
        # 目标目录尚未创建时向上找到最近的已存在目录再取容量，
        # 否则 disk_usage 抛 OSError 会退化成 0，被误判为磁盘已满。
        probe = directory
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
            return usage.free
        except OSError:
            return 0

    async def _unique_path(self, dst: Path, task_id: int) -> Path:
        stem, suffix = dst.stem, dst.suffix
        for index in range(1, 1000):
            candidate = dst.with_name(f"{stem} ({index}){suffix}")
            if candidate.exists() or Path(str(candidate) + ".part").exists():
                continue
            row = await self.db.fetchone(
                """
                SELECT 1 AS x FROM task
                WHERE dst_path = ? AND id != ?
                  AND status IN ('pending','resolving','downloading','verifying','paused')
                """,
                (str(candidate), task_id),
            )
            if row is None:
                return candidate
        raise DownloadError("同名文件过多，无法生成新文件名")

    async def _process(self, task_id: int) -> None:
        row = await self.db.fetchone("SELECT * FROM task WHERE id = ?", (task_id,))
        if not row or row["status"] != "pending" or task_id in self._paused:
            return

        if not await self._wait_schedule(task_id):
            return

        dst = Path(row["dst_path"])
        name = row["name"]
        size = int(row["size"] or 0)
        policy = self.config.get("on_conflict")

        if dst.exists():
            if policy == "skip":
                await self._set_status(
                    task_id, "skipped", error="目标文件已存在，已跳过", downloaded=dst.stat().st_size
                )
                return
            if policy == "keep_both":
                dst = await self._unique_path(dst, task_id)
                await self.db.execute(
                    "UPDATE task SET dst_path = ?, updated_at = ? WHERE id = ?",
                    (str(dst), _now(), task_id),
                )
                await self.publish(task_id)
                log.info("同名文件已另存为 %s", dst.name)

        if size > 0:
            free = self._free_space(dst.parent)
            watermark = float(self.config.get("low_watermark_gb") or 0)
            reserve = int(watermark * 1024 ** 3)
            if free - size < reserve:
                await self._fail(
                    task_id,
                    f"磁盘空间不足：需要 {size / 1024 ** 2:.0f} MB，剩余 {free / 1024 ** 2:.0f} MB，"
                    f"低于 {watermark:.1f} GB 保留水位",
                    retryable=False,
                )
                return

        stop_event = asyncio.Event()
        self._stop_events[task_id] = stop_event

        try:
            await self._set_status(task_id, "resolving", error=None, segments_json=None)
            client = await self.account.client()
            url = await client.get_download_url(row["fid"])

            if stop_event.is_set() or task_id in self._paused:
                raise Cancelled()

            await self._set_status(task_id, "downloading", error=None)
            await self._webhook(task_id, "started")

            last_db = 0.0
            last_push = 0.0

            async def progress(
                downloaded: int,
                total: int,
                counters: list[int],
                plan: list[tuple[int, int]],
            ) -> None:
                nonlocal last_db, last_push
                self._note_speed(task_id, downloaded)
                segments = [
                    {"start": start, "end": end, "done": counters[index]}
                    for index, (start, end) in enumerate(plan)
                ]
                now = time.monotonic()
                if now - last_push >= 1.0:
                    last_push = now
                    self.bus.publish(
                        {
                            "type": "progress",
                            "data": {
                                "id": task_id,
                                "name": name,
                                "downloaded": downloaded,
                                "size": total,
                                "segments": segments,
                            },
                        }
                    )
                if now - last_db >= 3.0:
                    last_db = now
                    await self.db.execute(
                        "UPDATE task SET downloaded = ?, size = ?, segments_json = ?,"
                        " updated_at = ? WHERE id = ?",
                        (downloaded, total, json.dumps(segments), _now(), task_id),
                    )

            await download_to(
                url=url,
                dst_path=dst,
                size=size,
                segments=self._segments_now(),
                stop_event=stop_event,
                progress=progress,
                retries=self.settings.retries,
                timeout=self.settings.request_timeout,
                cookie=client.cookie,
                limiter=self._limiter,
                adaptive=not self._risk_active(),
            )
            await self._set_status(
                task_id, "done", downloaded=dst.stat().st_size, error=None, segments_json=None
            )
            log.info("下载完成: %s", dst)
            await self._clear_risk()
            await self._webhook(task_id, "done")

        except Cancelled:
            if task_id in self._paused:
                await self._set_status(task_id, "paused", error=None)
                await self._webhook(task_id, "paused")
            else:
                await self._set_status(task_id, "canceled", error="已取消")
        except CookieExpired as exc:
            await self.account.mark_invalid()
            await self._set_status(
                task_id, "paused", error=f"Cookie 已失效，重新登录后可恢复：{exc.message}"
            )
            await self._webhook(task_id, "paused")
            self.bus.publish({"type": "account_invalid", "data": {}})
        except RiskControlError as exc:
            cooldown = await self._note_risk()
            if cooldown and self._risk_strikes >= 6:
                await self._fail(
                    task_id,
                    f"{exc}（已连续冷却 6 次，请稍后手动重试）",
                    retryable=False,
                )
                return
            delay = cooldown or RISK_FIRST_DELAY
            note = "已降低并发并" if cooldown else ""
            await self._set_status(
                task_id,
                "pending",
                error=f"{exc}，{note}冷却 {int(delay)} 秒后自动重试",
            )
            self._defer(task_id, delay)
        except DownloadError as exc:
            await self._fail(task_id, str(exc), retryable="磁盘" not in str(exc))
        except QuarkError as exc:
            await self._fail(task_id, f"夸克接口错误：{exc.message}")
        except Exception as exc:  # noqa: BLE001
            log.exception("下载任务 %s 失败", task_id)
            await self._fail(task_id, f"{type(exc).__name__}: {exc}")
        finally:
            self._stop_events.pop(task_id, None)
            self._speed_state.pop(task_id, None)

    def _auto_retry_limit(self) -> int:
        try:
            return max(0, int(self.config.get("auto_retry_count") or 0))
        except (TypeError, ValueError):
            return 0

    async def _webhook(self, task_id: int, event: str) -> None:
        url = str(self.config.get("webhook_url") or "").strip()
        if not url:
            return
        data = await self.snapshot(task_id)
        if not data:
            return
        payload = {"event": event, "sent_at": _now(), "task": data}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = str(self.config.get("webhook_secret") or "").strip()
        if secret:
            digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-QuarkNas-Signature"] = f"sha256={digest}"
        for attempt in range(1, 4):
            try:
                # Webhook 多为内网/本机地址，绕过系统代理避免被转发。
                async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                    response = await client.post(url, content=body, headers=headers)
                if response.status_code < 400:
                    log.info("Webhook 已推送 %s（HTTP %s）", event, response.status_code)
                    return
                log.warning(
                    "Webhook 推送 %s 返回 HTTP %s（第 %d/3 次）",
                    event,
                    response.status_code,
                    attempt,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Webhook 推送失败（第 %d/3 次）：%s", attempt, exc)
            if attempt < 3:
                await asyncio.sleep(attempt)

    async def _fail(self, task_id: int, error: str, retryable: bool = True) -> None:
        """任务失败入口：满足条件时按指数退避自动重试，否则标记失败。"""
        limit = self._auto_retry_limit()
        row = await self.db.fetchone("SELECT retry_count FROM task WHERE id = ?", (task_id,))
        count = int(row["retry_count"] or 0) if row else 0
        if retryable and count < limit:
            count += 1
            try:
                base = max(5, int(self.config.get("auto_retry_backoff") or 30))
            except (TypeError, ValueError):
                base = 30
            delay = min(600, base * 2 ** (count - 1))
            await self.db.execute(
                "UPDATE task SET retry_count = ? WHERE id = ?", (count, task_id)
            )
            await self._set_status(
                task_id, "pending", error=f"自动重试 {count}/{limit}（{delay} 秒后）：{error}"
            )
            self._defer(task_id, delay)
            log.info("任务 %s 失败，%d 秒后自动重试（第 %d/%d 次）", task_id, delay, count, limit)
            return
        await self._set_status(task_id, "failed", error=error)
        await self._webhook(task_id, "failed")

    async def retry(self, task_id: int) -> dict:
        row = await self.db.fetchone("SELECT status FROM task WHERE id = ?", (task_id,))
        if not row:
            return {"ok": False, "message": "任务不存在"}
        if row["status"] in BUSY_STATUSES:
            return {"ok": False, "message": "任务进行中或已暂停"}
        await self.db.execute(
            "UPDATE task SET status = 'pending', error = NULL, retry_count = 0,"
            " segments_json = NULL, updated_at = ? WHERE id = ?",
            (_now(), task_id),
        )
        await self.publish(task_id)
        await self.enqueue(task_id)
        return {"ok": True}
