from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from ..settings import DEFAULT_UA

log = logging.getLogger(__name__)

# 小于该体积的文件不分段；夸克直链对并发连接敏感，分段数有上限。
MIN_SEGMENT = 4 * 1024 * 1024
MAX_SEGMENTS = 8
CHUNK_SIZE = 64 * 1024
REFERER = "https://pan.quark.cn/"
MB = 1024 * 1024

# 低速自动扩容：慢源站常见「单连接被限死、总速度随连接数线性增长」，
# 此时把分段再一分为二。快节点单连接本身够快，不触发，避免无谓加连接。
ADAPTIVE_MAX_SEGMENTS = 16
ADAPTIVE_SAMPLE = 5.0
ADAPTIVE_MIN_PER_CONN = 0.35 * MB  # 单连接低于该值判定为被单连接限速
ADAPTIVE_TARGET_TOTAL = 3.0 * MB  # 总速度已达标则不再加连接
ADAPTIVE_MIN_PER_CONN_RATIO = 0.7  # 加连接后单连接速度跌破该比例，说明已到通道上限

# 连接轮换：夸克 CDN 对「单次 Range 跨度」敏感——实测同一连接、同一偏移下
# 8–256 MB 跨度约 2 MB/s，512 MB 以上骤降到 90 KB/s（分段动辄 1 GB 以上，直接踩坑）。
# 因此每次请求只取一个窗口，写完再开新连接续下一段。
RANGE_WINDOW = 64 * MB
CONN_SLOW_SECONDS = 60.0  # 连接建立后持续低速超过该时长也重连
CONN_SLOW_RATE = 0.3 * MB  # 低于该单连接速率视为已被降速
CONN_PROBE_SECONDS = 5.0  # 连接开局观察窗口：一开始就慢说明是慢节点，重连无益

# 错峰建连：多条连接同时拉起会在瞬间打满 TCP/TLS 握手，既浪费首包也更容易触发风控。
STAGGER_SECONDS = 0.025
STAGGER_CAP = 8

# 全进程在飞连接上限：并发任务 × 分段数最坏可到 64 条，夸克对单账号连接数敏感，
# 统一收口避免线程/内存膨胀与风控（对应 YunX 的 inflightLimiter）。
MAX_INFLIGHT_CONNECTIONS = 32
_inflight_slots = threading.BoundedSemaphore(MAX_INFLIGHT_CONNECTIONS)

# 慢连接抢占（对应 YunX 的看门狗）：每 5 秒采样一次在飞连接，
# 极慢的连接主动断开换一条续传，冷却与次数上限避免反复抖动。
PREEMPT_TICK = 5.0
PREEMPT_MIN_BPS = 12 * 1024  # 低于该瞬时速率视为卡住
PREEMPT_MIN_AGE = 15.0  # 连接至少跑这么久才允许判定
PREEMPT_COOLDOWN = 10.0  # 同一分段两次抢占的最小间隔
PREEMPT_MIN_REMAIN = 128 * 1024  # 剩余量太少就不折腾了
PREEMPT_MAX_PER_SEGMENT = 3  # 单个分段累计抢占上限
PREEMPT_PER_TICK = 2  # 每轮最多抢占几路
PREEMPT_ENDGAME_WORKERS = 3  # 收尾阶段只剩少量连接时
PREEMPT_ENDGAME_AGE = 3.0  # 观察窗口缩短，避免尾巴拖太久

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")

ProgressCb = Callable[[int, int, list[int], list[tuple[int, int]]], Awaitable[None]]


class DownloadError(Exception):
    """不可重试的错误，任务直接失败。"""


class RiskControlError(DownloadError):
    """直链返回 403：多半是并发过高触发风控，交给队列降并发并冷却。"""


class RetryableError(Exception):
    """网络抖动类错误，可在分段内重试。"""


class Cancelled(Exception):
    """用户取消。"""


def _parse_content_range(value: str) -> tuple[int, int, int] | None:
    """解析 Content-Range，返回 (起点, 终点, 总长)；总长为 * 时记 0。"""
    match = _CONTENT_RANGE_RE.fullmatch((value or "").strip().lower())
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    total = 0 if match.group(3) == "*" else int(match.group(3))
    if end < start or (total and end >= total):
        return None
    return start, end, total


def _is_html(resp: httpx.Response) -> bool:
    """CDN 在直链失效或防盗链时会返回 200 的 HTML 页面，不能当数据写入。"""
    return "text/html" in (resp.headers.get("content-type") or "").lower()


def _acquire_slot(stop: threading.Event) -> None:
    """占用一条全局连接配额，等待期间保持可取消。"""
    while True:
        if stop.is_set():
            raise Cancelled()
        if _inflight_slots.acquire(timeout=0.5):
            return


class _SegmentState:
    """分段当前连接的状态，供看门狗采样与抢占。"""

    __slots__ = ("preempt", "started_at", "bytes", "preempts", "last_preempt_at", "active")

    def __init__(self) -> None:
        self.preempt = threading.Event()
        self.started_at = time.monotonic()
        self.bytes = 0
        self.preempts = 0
        self.last_preempt_at = 0.0
        self.active = False

    def begin(self) -> None:
        """开始一个新窗口：清掉上一次的抢占信号并重置采样。"""
        self.preempt.clear()
        self.started_at = time.monotonic()
        self.bytes = 0
        self.active = True

    def end(self) -> None:
        self.active = False


class RateLimiter:
    """跨任务共享的限速器：rate 为字节/秒，0 表示不限速，可运行时调整。"""

    def __init__(self, rate: float = 0.0) -> None:
        self.rate = max(0.0, rate)
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def set_rate(self, rate: float) -> None:
        self.rate = max(0.0, rate)

    def consume(self, size: int) -> None:
        rate = self.rate
        if rate <= 0 or size <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._next < now:
                self._next = now
            self._next += size / rate
            delay = self._next - now
        if delay > 0:
            time.sleep(delay)


def plan_segments(size: int, segments: int) -> list[tuple[int, int]]:
    if size <= 0:
        raise DownloadError("文件大小未知，无法分段")
    segments = max(1, min(MAX_SEGMENTS, segments))
    if size < MIN_SEGMENT:
        segments = 1
    segments = min(segments, max(1, math.ceil(size / MIN_SEGMENT)))
    step = math.ceil(size / segments)
    plan: list[tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(start + step - 1, size - 1)
        plan.append((start, end))
        start = end + 1
    return plan


def _segment_worker(
    url: str,
    part_path: str,
    idx: int,
    start: int,
    already: int,
    limits: list[int],
    stop: threading.Event,
    push: Callable[[int], None],
    state: _SegmentState,
    *,
    retries: int,
    timeout: float,
    cookie: str,
    limiter: RateLimiter | None = None,
    start_delay: float = 0.0,
) -> None:
    """同步分段下载：从 start+already 写到 limits[idx]（闭区间）。

    limits[idx] 可被扩容逻辑在运行中缩短，工作线程每写一块都会重新读取。
    start_delay 用于错峰建连，避免多条连接同时发起握手。
    """
    if start_delay > 0 and stop.wait(start_delay):
        raise Cancelled()
    pos = start + already
    attempt = 0
    slow_node = False  # 该分段所在节点本身慢：不再按低速反复重连
    headers = {"User-Agent": DEFAULT_UA, "Referer": REFERER}
    if cookie:
        headers["Cookie"] = cookie
    while pos <= limits[idx]:
        if stop.is_set():
            raise Cancelled()
        # 每次只请求一个窗口：跨度太大（≥512 MB）会被 CDN 压到 90 KB/s。
        hi = min(pos + RANGE_WINDOW - 1, limits[idx])
        rotated = False
        probed = False
        acquired = False
        try:
            _acquire_slot(stop)
            acquired = True
            state.begin()
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=10.0),
                follow_redirects=True,
            ) as client:
                with client.stream(
                    "GET", url, headers={**headers, "Range": f"bytes={pos}-{hi}"}
                ) as resp:
                    if resp.status_code == 412:
                        raise DownloadError("直链返回 412：会话缺少 CDN 鉴权 Cookie，请重新登录")
                    if resp.status_code == 403:
                        raise RiskControlError("直链返回 403，触发夸克风控或 Cookie 失效")
                    if resp.status_code in (404, 410):
                        raise DownloadError("直链已过期（404/410）")
                    if resp.status_code == 200 and pos != 0:
                        raise RetryableError("服务器忽略了 Range 请求")
                    if resp.status_code not in (200, 206):
                        raise RetryableError(f"HTTP {resp.status_code}")
                    if _is_html(resp):
                        raise DownloadError("直链返回 HTML 页面（防盗链或登录已过期）")
                    # 206 必须落在请求的起点上，否则数据会写错位：宁可重试也不落盘。
                    range_end = hi
                    if resp.status_code == 206:
                        span = _parse_content_range(resp.headers.get("content-range", ""))
                        if span is None:
                            raise RetryableError("206 响应缺少合法的 Content-Range")
                        if span[0] != pos:
                            raise RetryableError(
                                f"Content-Range 起点 {span[0]} 与请求 {pos} 不一致"
                            )
                        range_end = min(span[1], hi)

                    with open(part_path, "r+b") as fh:
                        fh.seek(pos)
                        for chunk in resp.iter_bytes(CHUNK_SIZE):
                            if stop.is_set():
                                raise Cancelled()
                            end = min(range_end, limits[idx])  # 扩容可能在中途缩短该分段
                            if pos > end:
                                break
                            if not chunk:
                                continue
                            remaining = end - pos + 1
                            if len(chunk) > remaining:
                                chunk = chunk[:remaining]
                            fh.write(chunk)
                            pos += len(chunk)
                            state.bytes += len(chunk)
                            push(len(chunk))
                            if limiter is not None:
                                limiter.consume(len(chunk))
                            if state.preempt.is_set():
                                rotated = True  # 看门狗判定卡慢，保留已写字节换连接
                                break
                            if pos > end:
                                break
                            # 限速期不判低速，否则限速本身会把连接当成「被降速」反复重连。
                            if limiter is not None and limiter.rate > 0:
                                continue
                            elapsed = time.monotonic() - state.started_at
                            if not probed and elapsed >= CONN_PROBE_SECONDS:
                                probed = True
                                # 开局就慢 = 慢节点，重连也快不起来，转由自动扩容加连接。
                                slow_node = state.bytes < CONN_SLOW_RATE * elapsed
                            if (
                                not slow_node
                                and elapsed >= CONN_SLOW_SECONDS
                                and state.bytes < CONN_SLOW_RATE * elapsed
                            ):
                                rotated = True
                                break
            if rotated:
                attempt = 0  # 主动轮换不算失败，不消耗重试次数
                continue
            if pos > limits[idx]:
                continue  # 分段被扩容缩短，已越过终点，外层循环会结束
            if pos > range_end:
                attempt = 0  # 本次窗口已收完整（服务器可能返回比请求更短的区间）
                continue
            raise RetryableError("连接提前关闭，未写满本窗口")
        except (RetryableError, httpx.HTTPError) as exc:
            if acquired:
                _inflight_slots.release()  # 退避期间不占连接配额
                acquired = False
            attempt += 1
            if attempt >= retries:
                raise DownloadError(f"分段下载失败：{exc}") from exc
            stop.wait(min(2 ** attempt, 15))  # 可被取消打断，不必睡满
        finally:
            state.end()
            if acquired:
                _inflight_slots.release()


async def probe_size(url: str, timeout: float, cookie: str = "") -> int:
    def _probe() -> int:
        headers = {"User-Agent": DEFAULT_UA, "Referer": REFERER, "Range": "bytes=0-0"}
        if cookie:
            headers["Cookie"] = cookie
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True) as c:
            with c.stream("GET", url, headers=headers) as resp:
                if _is_html(resp):
                    raise DownloadError("直链返回 HTML 页面（防盗链或登录已过期）")
                if resp.status_code == 206:
                    span = _parse_content_range(resp.headers.get("content-range", ""))
                    return span[2] if span is not None else 0
                if resp.status_code == 200:
                    return int(resp.headers.get("content-length") or 0)
                if resp.status_code == 412:
                    raise DownloadError("直链返回 412：会话缺少 CDN 鉴权 Cookie，请重新登录")
                raise DownloadError(f"直链探测失败 HTTP {resp.status_code}")

    return await asyncio.to_thread(_probe)


def _write_meta(meta_path: Path, total: int, plan: list[tuple[int, int]], done: list[int]) -> None:
    payload = {
        "total": total,
        "plan": [list(p) for p in plan],
        "done": list(done),
        "updated_at": int(time.time()),
    }
    tmp = Path(str(meta_path) + ".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, meta_path)
    except OSError:
        log.debug("写入断点信息失败", exc_info=True)


def _load_meta(
    meta_path: Path, total: int
) -> tuple[list[tuple[int, int]], list[int]] | None:
    """读取断点信息。分段方案可能因自动扩容而变化，因此以文件里记录的方案为准。"""
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("total") != total:
        return None
    raw_plan = payload.get("plan")
    raw_done = payload.get("done")
    if not isinstance(raw_plan, list) or not isinstance(raw_done, list):
        return None
    if not raw_plan or len(raw_plan) != len(raw_done):
        return None
    plan: list[tuple[int, int]] = []
    cursor = 0
    for item in raw_plan:
        if not isinstance(item, list) or len(item) != 2:
            return None
        try:
            start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError):
            return None
        if start != cursor or end < start or end >= total:
            return None
        plan.append((start, end))
        cursor = end + 1
    if cursor != total:
        return None
    done: list[int] = []
    for value, (start, end) in zip(raw_done, plan):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 0
        done.append(max(0, min(value, end - start + 1)))
    return plan, done


async def download_to(
    *,
    url: str,
    dst_path: Path,
    size: int,
    segments: int,
    stop_event: asyncio.Event | None,
    progress: ProgressCb | None,
    retries: int = 3,
    timeout: float = 30.0,
    cookie: str = "",
    limiter: RateLimiter | None = None,
    adaptive: bool = True,
) -> int:
    """下载直链到 dst_path，返回最终字节数。支持断点续传、原子落盘与低速自动扩容。

    adaptive=False 时不做低速扩容（风控冷却期使用，避免继续加连接）。
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(dst_path) + ".part")
    meta_path = Path(str(dst_path) + ".part.json")

    total = size if size > 0 else await probe_size(url, timeout, cookie)
    if total <= 0:
        raise DownloadError("无法获取文件大小")

    loaded = _load_meta(meta_path, total) if part.exists() else None
    if loaded is not None:
        plan, initial = loaded
    else:
        plan, initial = plan_segments(total, segments), None

    if not part.exists():
        with open(part, "wb") as fh:
            fh.truncate(total)
    elif part.stat().st_size != total:
        with open(part, "r+b") as fh:
            fh.truncate(total)
    if initial is None:
        initial = [0] * len(plan)

    lock = threading.Lock()
    counters = list(initial)
    limits = [end for _start, end in plan]
    threading_stop = threading.Event()
    workers: dict[int, asyncio.Task] = {}
    states = [_SegmentState() for _ in plan]

    def counts() -> list[int]:
        with lock:
            return [
                min(counters[i], max(0, limits[i] - plan[i][0] + 1))
                for i in range(len(plan))
            ]

    def make_push(idx: int) -> Callable[[int], None]:
        def push(n: int) -> None:
            with lock:
                length = limits[idx] - plan[idx][0] + 1
                counters[idx] = min(counters[idx] + n, max(0, length))

        return push

    def spawn(idx: int) -> None:
        start = plan[idx][0]
        workers[idx] = asyncio.create_task(
            asyncio.to_thread(
                _segment_worker,
                url,
                str(part),
                idx,
                start,
                counters[idx],
                limits,
                threading_stop,
                make_push(idx),
                states[idx],
                retries=retries,
                timeout=timeout,
                cookie=cookie,
                limiter=limiter,
                start_delay=min(idx, STAGGER_CAP) * STAGGER_SECONDS,
            )
        )

    def expand() -> bool:
        """把剩余量最大的分段从中间切开，后半段交给新连接。"""
        with lock:
            if len(plan) >= ADAPTIVE_MAX_SEGMENTS:
                return False
            candidates = [
                i
                for i in range(len(plan))
                if limits[i] - (plan[i][0] + counters[i]) + 1 >= 2 * MIN_SEGMENT
            ]
            if not candidates:
                return False
            idx = max(candidates, key=lambda i: limits[i] - (plan[i][0] + counters[i]))
            end = limits[idx]
            mid = plan[idx][0] + counters[idx] + (end - plan[idx][0] - counters[idx] + 1) // 2
            limits[idx] = mid - 1
            counters[idx] = min(counters[idx], mid - plan[idx][0])
            plan.append((mid, end))
            counters.append(0)
            limits.append(end)
            states.append(_SegmentState())
            new_idx = len(plan) - 1
        spawn(new_idx)
        log.info("低速扩容：连接数增至 %d", len(plan))
        return True

    def preempt_slow(now: float) -> None:
        """看门狗：把卡在极低速的在飞连接换掉，保留已写字节续传。"""
        active = [idx for idx, task in workers.items() if not task.done()]
        if not active:
            return
        # 收尾阶段连接少、剩余量小，缩短观察窗口，避免尾巴被一条卡死的连接拖住。
        min_age = (
            PREEMPT_ENDGAME_AGE
            if len(active) <= PREEMPT_ENDGAME_WORKERS
            else PREEMPT_MIN_AGE
        )
        candidates: list[tuple[float, int]] = []
        with lock:
            for idx in active:
                state = states[idx]
                if not state.active:  # 还在等全局配额，不算在飞
                    continue
                age = now - state.started_at
                remain = limits[idx] - (plan[idx][0] + counters[idx]) + 1
                if age < min_age or remain < PREEMPT_MIN_REMAIN:
                    continue
                if state.preempts >= PREEMPT_MAX_PER_SEGMENT:
                    continue
                if now - state.last_preempt_at < PREEMPT_COOLDOWN:
                    continue
                rate = state.bytes / age
                if rate < PREEMPT_MIN_BPS:
                    candidates.append((rate, idx))
        for rate, idx in sorted(candidates)[:PREEMPT_PER_TICK]:
            state = states[idx]
            state.preempts += 1
            state.last_preempt_at = now
            state.preempt.set()
            log.info(
                "卡慢连接：分段 %d 速率 %.1f KB/s，换连接续传（第 %d 次）",
                idx,
                rate / 1024,
                state.preempts,
            )

    for idx in range(len(plan)):
        spawn(idx)

    if progress:
        await progress(sum(counts()), total, counts(), list(plan))

    last_flush = 0.0
    last_sample = time.monotonic()
    last_preempt_scan = time.monotonic()
    last_bytes = sum(counts())
    expand_baseline: float | None = None
    expand_blocked = False
    can_expand = adaptive and (limiter is None or limiter.rate <= 0)

    try:
        while workers:
            if stop_event is not None and stop_event.is_set():
                threading_stop.set()
            await asyncio.wait(list(workers.values()), timeout=1.0)

            downloaded = sum(counts())
            if progress:
                await progress(downloaded, total, counts(), list(plan))

            now = time.monotonic()
            if now - last_flush >= 3.0:
                _write_meta(meta_path, total, plan, counters)
                last_flush = now

            failure = None
            for idx, task in list(workers.items()):
                if not task.done():
                    continue
                del workers[idx]
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None and not isinstance(exc, Cancelled):
                    failure = exc
                    break
            if failure is not None:
                threading_stop.set()
                await asyncio.gather(*workers.values(), return_exceptions=True)
                _write_meta(meta_path, total, plan, counters)
                raise failure

            if stop_event is not None and stop_event.is_set():
                continue

            if can_expand and not expand_blocked and now - last_sample >= ADAPTIVE_SAMPLE:
                elapsed = max(now - last_sample, 1e-6)
                speed = (downloaded - last_bytes) / elapsed
                per_conn = speed / len(workers) if workers else 0.0
                if expand_baseline is not None:
                    if per_conn < expand_baseline * ADAPTIVE_MIN_PER_CONN_RATIO:
                        expand_blocked = True
                        log.info(
                            "扩容后单连接速度下降（%.2f MB/s），已接近通道上限，保持 %d 个连接",
                            per_conn / MB,
                            len(workers),
                        )
                    expand_baseline = None
                elif speed < ADAPTIVE_TARGET_TOTAL and per_conn < ADAPTIVE_MIN_PER_CONN:
                    before = per_conn
                    if expand():
                        expand_baseline = before
                last_sample = now
                last_bytes = downloaded

            # 限速期不抢占：采样速率里含限速影响，会把正常连接误判成卡死。
            if (
                (limiter is None or limiter.rate <= 0)
                and now - last_preempt_scan >= PREEMPT_TICK
            ):
                last_preempt_scan = now
                preempt_slow(now)
    finally:
        threading_stop.set()

    _write_meta(meta_path, total, plan, counters)

    if stop_event is not None and stop_event.is_set():
        raise Cancelled()

    downloaded = sum(counts())
    if downloaded != total:
        raise DownloadError(f"数据不完整：{downloaded}/{total}")

    os.replace(part, dst_path)
    try:
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass
    return downloaded
