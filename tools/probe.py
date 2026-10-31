#!/usr/bin/env python3
"""M0 探测脚本：验证夸克网盘 Cookie、目录浏览、直链、Range 与多连接速度。

用法（只需 httpx，不进容器）：

    pip install httpx
    python tools/probe.py --cookie-file cookie.txt
    python tools/probe.py --cookie "<粘贴的 Cookie>" --speed-mb 32 --threads 8
    python tools/probe.py --cookie-file cookie.txt --resume-test

所有输出都会告诉你：Cookie 是否可用、直链是否支持 Range（决定能否续传/分段）、
单连接与多连接的真实速度，以及断点续传是否生效。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
    "quark-cloud-drive/3.14.2 Channel/pckk_other_ch"
)
DRIVE_HOSTS = ("https://drive-pc.quark.cn", "https://drive-m.quark.cn")
LIST_PATH = "/1/clouddrive/file/sort"
DOWNLOAD_PATH = "/1/clouddrive/file/download"
# 直链除了签名，还要用 __puus 做二次鉴权，缺了必 412。
SESSION_PATH = "/1/clouddrive/member"
SESSION_COOKIE = "__puus"
REFERER = "https://pan.quark.cn/"

# 夸克各接口的成功标记并不统一：drive 给 status=200，通行证给 code="OK"/2000000。
SUCCESS_STATUS = {0, 200, 2000000}


def _ok(payload: dict) -> bool:
    raw_code = payload.get("code")
    if isinstance(raw_code, str):
        text = raw_code.strip().lower()
        if text in ("ok", "success", ""):
            code = 0
        else:
            try:
                code = int(text)
            except ValueError:
                return False
    elif raw_code is None:
        code = 0
    else:
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = 0

    try:
        status = int(payload.get("status") or 0)
    except (TypeError, ValueError):
        status = 0

    if payload.get("success") is False:
        return False
    return code == 0 and status in SUCCESS_STATUS


@dataclass
class Probe:
    cookie: str
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self.headers = {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ API

    def refresh_session(self) -> None:
        """补抓 __puus：扫码登录拿到的 Cookie 里没有它，直链会 412。"""
        with httpx.Client(timeout=self.timeout, headers=self.headers, follow_redirects=True) as c:
            resp = c.get(
                DRIVE_HOSTS[0] + SESSION_PATH,
                params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"刷新会话失败 HTTP {resp.status_code}")
        jar = dict(
            part.split("=", 1)
            for part in (p.strip() for p in self.cookie.split(";"))
            if "=" in part
        )
        for raw in resp.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            jar[name.strip()] = rest.split(";")[0]
        self.cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
        self.headers["Cookie"] = self.cookie

    def _json(self, method: str, path: str, **kwargs: Any) -> dict:
        last_error: Exception | None = None
        for host in DRIVE_HOSTS:
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=self.headers) as c:
                    resp = c.request(method, host + path, **kwargs)
                payload = resp.json()
                if resp.status_code >= 400:
                    last_error = RuntimeError(f"HTTP {resp.status_code}: {payload}")
                    continue
                if not _ok(payload):
                    raise RuntimeError(
                        f"业务错误 code={payload.get('code')} status={payload.get('status')}"
                        f" message={payload.get('message')}"
                    )
                return payload
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                continue
        raise RuntimeError(f"接口不可用：{last_error}")

    def account(self) -> dict:
        with httpx.Client(timeout=self.timeout, headers=self.headers, follow_redirects=True) as c:
            resp = c.get("https://pan.quark.cn/account/info", params={"fr": "pc", "platform": "pc"})
        payload = resp.json()
        if not _ok(payload):
            raise RuntimeError(
                f"取账号信息失败 code={payload.get('code')} status={payload.get('status')}"
                f" message={payload.get('message')}"
            )
        return payload.get("data") or {}

    def member(self) -> dict:
        return self._json(
            "GET",
            SESSION_PATH,
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
        ).get("data") or {}

    def list_dir(self, fid: str = "0", page: int = 1, size: int = 50) -> dict:
        return self._json(
            "GET",
            LIST_PATH,
            params={
                "pr": "ucpro",
                "fr": "pc",
                "pdir_fid": fid,
                "_page": page,
                "_size": size,
                "_fetch_total": 1,
                "_sort": "file_type:asc,updated_at:desc",
                "uc_param_str": "",
            },
        )

    def download_url(self, fid: str) -> str:
        payload = self._json(
            "POST",
            DOWNLOAD_PATH,
            params={"pr": "ucpro", "fr": "pc"},
            content=json.dumps({"fids": [fid]}),
        )
        for item in payload.get("data") or []:
            if item.get("download_url"):
                return item["download_url"]
        raise RuntimeError("未拿到直链（可能触发风控）")


# ---------------------------------------------------------------------- 测速


def range_probe(url: str, timeout: float, cookie: str = "") -> tuple[int, bool, dict]:
    headers = {"User-Agent": UA, "Referer": REFERER, "Range": "bytes=0-0"}
    if cookie:
        headers["Cookie"] = cookie
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True) as c:
        with c.stream("GET", url, headers=headers) as resp:
            info = {
                "status": resp.status_code,
                "content_range": resp.headers.get("content-range"),
                "content_type": resp.headers.get("content-type"),
                "accept_ranges": resp.headers.get("accept-ranges"),
                "content_length": resp.headers.get("content-length"),
            }
            if resp.status_code == 206:
                total = (info["content_range"] or "").rsplit("/", 1)[-1]
                return int(total) if total.isdigit() else 0, True, info
            return int(info["content_length"] or 0), False, info


def _fetch(url: str, start: int, end: int, timeout: float, result: dict, key: str,
           cookie: str = "") -> None:
    headers = {"User-Agent": UA, "Referer": REFERER, "Range": f"bytes={start}-{end}"}
    if cookie:
        headers["Cookie"] = cookie
    written = 0
    started = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True) as c:
            with c.stream("GET", url, headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    result[key] = {"error": f"HTTP {resp.status_code}"}
                    return
                for chunk in resp.iter_bytes(64 * 1024):
                    written += len(chunk)
    except httpx.HTTPError as exc:
        result[key] = {"error": str(exc)}
        return
    elapsed = max(time.monotonic() - started, 0.001)
    result[key] = {"bytes": written, "seconds": elapsed, "mbps": written / elapsed / 1024 / 1024}


def speed_test(url: str, size: int, window_bytes: int, threads: int, timeout: float,
               cookie: str = "") -> dict:
    window = min(window_bytes, size) if size > 0 else window_bytes

    single = {}
    _fetch(url, 0, window - 1, timeout, single, "single", cookie)

    result = {"window": window, "single": single.get("single", {})}
    if threads <= 1:
        return result

    step = window // threads
    shared: dict[str, Any] = {}
    started = time.monotonic()
    workers = []
    for index in range(threads):
        start = index * step
        end = window - 1 if index == threads - 1 else start + step - 1
        thread = threading.Thread(
            target=_fetch, args=(url, start, end, timeout, shared, f"t{index}", cookie), daemon=True
        )
        workers.append(thread)
        thread.start()
    for thread in workers:
        thread.join()
    elapsed = max(time.monotonic() - started, 0.001)
    total = sum(item.get("bytes", 0) for item in shared.values())
    errors = [item["error"] for item in shared.values() if "error" in item]
    result["multi"] = {
        "bytes": total,
        "seconds": elapsed,
        "mbps": total / elapsed / 1024 / 1024,
        "errors": errors,
    }
    return result


def resume_test(url: str, timeout: float, part_bytes: int = 8 * 1024 * 1024,
                cookie: str = "") -> dict:
    headers = {"User-Agent": UA, "Referer": REFERER}
    if cookie:
        headers["Cookie"] = cookie
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True) as c:
        with c.stream("GET", url, headers={**headers, "Range": f"bytes=0-{part_bytes - 1}"}) as resp:
            if resp.status_code != 206:
                return {"ok": False, "reason": f"首段返回 {resp.status_code}，不支持 Range"}
            first = 0
            for chunk in resp.iter_bytes(1024 * 1024):
                first += len(chunk)
        with c.stream("GET", url, headers={**headers, "Range": f"bytes={first}-{first + 1024 * 1024 - 1}"}) as resp:
            if resp.status_code != 206:
                return {"ok": False, "reason": f"续传请求返回 {resp.status_code}"}
            content_range = resp.headers.get("content-range") or ""
            matched = content_range.startswith(f"bytes {first}-")
            got = 0
            for chunk in resp.iter_bytes(1024 * 1024):
                got += len(chunk)
    return {"ok": matched, "offset": first, "content_range": content_range, "bytes": got}


# ---------------------------------------------------------------------- 展示


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def choose_file(probe: Probe, args: argparse.Namespace) -> dict:
    if args.fid:
        return {"fid": args.fid, "file_name": args.fid, "size": 0, "dir": False}
    payload = probe.list_dir("0", 1, 200)
    entries = payload.get("data", {}).get("list") or []
    files = [e for e in entries if not e.get("dir")]
    if not files:
        raise SystemExit("根目录没有文件，请用 --fid 指定具体文件")
    files.sort(key=lambda e: int(e.get("size") or 0), reverse=True)
    return files[min(args.pick, len(files) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description="夸克网盘下载能力探测（M0）")
    parser.add_argument("--cookie", help="直接传入 Cookie 字符串")
    parser.add_argument("--cookie-file", help="从文件读取 Cookie")
    parser.add_argument("--fid", help="指定探测的文件 fid，默认取根目录最大的文件")
    parser.add_argument("--pick", type=int, default=0, help="根目录按大小倒序取第 N 个文件（从 0 开始）")
    parser.add_argument("--speed-mb", type=int, default=32, help="测速窗口大小（MB），默认 32")
    parser.add_argument("--threads", type=int, default=8, help="多连接测速的并发数，默认 8")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--resume-test", action="store_true", help="做一次断点续传验证")
    parser.add_argument("--json", help="把结果写入指定 JSON 文件")
    args = parser.parse_args()

    cookie = args.cookie or os.environ.get("QUARK_COOKIE", "")
    if args.cookie_file:
        with open(args.cookie_file, "r", encoding="utf-8") as fh:
            cookie = fh.read()
    cookie = (cookie or "").strip()
    if not cookie:
        parser.error("需要 --cookie / --cookie-file / 环境变量 QUARK_COOKIE")

    report: dict[str, Any] = {"time": time.strftime("%Y-%m-%d %H:%M:%S")}
    probe = Probe(cookie, args.timeout)

    print("=" * 64)
    print("1) 账号信息")
    try:
        info = probe.account()
    except Exception as exc:  # noqa: BLE001
        print(f"   ✗ 无法获取账号信息：{exc}")
        print("   常见原因：Cookie 不完整或已过期、网络不通、被代理拦截")
        print("   请确认浏览器已登录 pan.quark.cn，并复制完整 Cookie（含 __puus、__pus 等字段）")
        return 2
    print(f"   昵称: {info.get('nickname') or '(空)'}")
    report["account"] = {"nickname": info.get("nickname")}

    try:
        meta = probe.member()
        member_type = (meta.get("member_type") or "").upper()
        exp_at = int(meta.get("exp_at") or 0) // 1000
        exp_text = time.strftime("%Y-%m-%d", time.localtime(exp_at)) if exp_at else "无"
        label = {"EXP_SVIP": "体验 SVIP", "SVIP": "SVIP", "VIP": "VIP",
                 "Z_VIP": "Z-VIP", "MINI_VIP": "迷你会员"}.get(member_type)
        if not member_type or member_type == "NONE" or not exp_at:
            print("   会员: 非会员（大文件可能被限速）")
        elif exp_at < time.time():
            print(f"   会员: {label or member_type}（已于 {exp_text} 过期）")
        else:
            print(f"   会员: {label or member_type}（到期 {exp_text}）")
        report["account"].update({"member_type": member_type or "NONE", "exp_at": exp_text})
    except Exception as exc:  # noqa: BLE001
        print(f"   ! 取会员信息失败：{exc}")

    try:
        probe.refresh_session()
        print(f"   会话: 已补齐 CDN 鉴权 Cookie（含 __puus）")
    except Exception as exc:  # noqa: BLE001
        print(f"   ! 刷新会话失败，直链可能 412：{exc}")

    print("\n2) 目录浏览")
    try:
        listing = probe.list_dir("0", 1, 20)
    except Exception as exc:  # noqa: BLE001
        print(f"   ✗ 列目录失败：{exc}")
        return 2
    entries = listing.get("data", {}).get("list") or []
    total = (listing.get("metadata") or {}).get("_total")
    print(f"   根目录总数: {total}，本页 {len(entries)} 项")
    for entry in entries[:10]:
        kind = "目录" if entry.get("dir") else human(int(entry.get("size") or 0))
        print(f"   - [{kind}] {entry.get('file_name')}")
    report["listing"] = {"total": total}

    target = choose_file(probe, args)
    print("\n3) 直链")
    print(f"   目标文件: {target.get('file_name')} ({human(int(target.get('size') or 0))})")
    try:
        url = probe.download_url(target["fid"])
    except Exception as exc:  # noqa: BLE001
        print(f"   ✗ 获取直链失败：{exc}")
        print("   如果根目录没有文件，请用 --fid 指定具体文件")
        return 2
    host = url.split("/")[2] if "//" in url else url[:40]
    print(f"   直链主机: {host}  长度 {len(url)} 字符")
    report["download_host"] = host

    print("\n4) Range 支持（决定能否分段/续传）")
    size, supports_range, raw = range_probe(url, args.timeout, probe.cookie)
    print(f"   HTTP 状态: {raw['status']}   Accept-Ranges: {raw['accept_ranges']}")
    print(f"   Content-Range: {raw['content_range']}")
    print(f"   文件大小: {human(size)}")
    print(f"   结论: {'支持 206 分段下载' if supports_range else '不支持 Range，只能单连接顺序下载'}")
    report["range"] = {"supported": supports_range, "size": size, "headers": raw}

    if size > 0:
        print("\n5) 速度对比")
        stats = speed_test(url, size, args.speed_mb * 1024 * 1024, args.threads, args.timeout,
                           probe.cookie)
        single = stats.get("single", {})
        print(f"   单连接: {single.get('mbps', 0):.2f} MB/s  窗口 {human(stats['window'])}")
        multi = stats.get("multi")
        if multi:
            print(f"   {args.threads} 连接: {multi.get('mbps', 0):.2f} MB/s  错误 {multi.get('errors')}")
            gain = multi.get("mbps", 0) / max(single.get("mbps", 0.001), 0.001)
            print(f"   提速倍数: {gain:.2f}x")
        report["speed"] = stats

    if args.resume_test:
        print("\n6) 断点续传验证")
        result = resume_test(url, args.timeout, cookie=probe.cookie)
        print(f"   {json.dumps(result, ensure_ascii=False)}")
        report["resume"] = result

    print("\n" + "=" * 64)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
