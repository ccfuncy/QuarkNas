"""P0 端到端测试：暂停 / 恢复 / 取消 + 任务状态校验。

前提：QuarkNas 服务已启动，且账号 Cookie 有效。
用法：
    .venv\\Scripts\\python.exe tools\\e2e_p0.py
环境变量：
    QUARKNAS_URL  服务地址，默认 http://127.0.0.1:8099

注意状态校验的顺序：必须先在已取消的任务上校验 400，再执行 retry，
否则 retry 会把任务重新拉回 downloading，校验就失去意义。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("QUARKNAS_URL", "http://127.0.0.1:8099").rstrip("/")
TEST_DIR = "_e2e_state"
MB = 1024 * 1024

results: list[tuple[str, bool, str]] = []
# 本机测试直连，忽略系统代理（否则 127.0.0.1 可能被代理拦截成 502）。
client = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def api(method: str, path: str, expect: int = 200, **kwargs):
    resp = client.request(method, path, **kwargs)
    if resp.status_code != expect:
        raise AssertionError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp


def get_settings() -> dict:
    return api("GET", "/api/settings").json()["values"]


def put_settings(**values) -> None:
    api("PUT", "/api/settings", json=values)


def get_tasks() -> list[dict]:
    return api("GET", "/api/tasks").json()["tasks"]


def get_task(tid: int) -> dict:
    for item in get_tasks():
        if item["id"] == tid:
            return item
    raise AssertionError(f"任务 {tid} 不存在")


def wait_task(tid: int, statuses: set[str], timeout: float = 40.0, downloaded_gt: int | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = get_task(tid)
        if item["status"] in statuses and (downloaded_gt is None or item["downloaded"] > downloaded_gt):
            return item
        if item["status"] in {"failed", "canceled", "done", "skipped"} and item["status"] not in statuses:
            raise AssertionError(f"任务 {tid} 意外进入 {item['status']}: {item.get('error')}")
        time.sleep(0.3)
    raise AssertionError(f"等待任务 {tid} 超时，最后状态 {get_task(tid)['status']}")


def expect_error(method: str, path: str, keyword: str) -> None:
    resp = client.request(method, path)
    ok = resp.status_code == 400 and keyword in resp.text
    check(f"{method} {path} 被拒绝", ok, f"HTTP {resp.status_code} {resp.text[:120]}")


def main() -> int:
    original = get_settings()
    tid: int | None = None
    dst_parent: Path | None = None
    try:
        entries = api("GET", "/api/files", params={"path": "/"}).json()["entries"]
        files = sorted((e for e in entries if not e["dir"] and e["size"] > 0), key=lambda e: e["size"])
        target = next((e for e in reversed(files) if e["size"] >= 10 * MB), files[-1] if files else None)
        if target is None:
            raise AssertionError("云盘根目录没有可用于测试的文件")

        put_settings(segments=2, concurrency=1, speed_limit_mbps=0.3, on_conflict="skip")
        payload = {
            "items": [{
                "fid": target["fid"],
                "name": target["name"],
                "dir": False,
                "size": target["size"],
                "path": f"/{TEST_DIR}",
            }],
            "keep_structure": True,
        }
        tid = api("POST", "/api/tasks", json=payload).json()["ids"][0]
        print(f"测试任务 id={tid} 文件={target['name']} 大小={target['size']} 字节\n")

        started = wait_task(tid, {"downloading"}, downloaded_gt=0)
        check("任务进入 downloading 且有进度", True, f"{started['downloaded']} 字节")

        api("POST", f"/api/tasks/{tid}/pause")
        paused = wait_task(tid, {"paused"})
        paused_dl = paused["downloaded"]
        check("暂停后状态为 paused", True, f"{paused_dl} 字节")

        time.sleep(2)
        again = get_task(tid)
        check("暂停后进度不再增长", again["downloaded"] == paused_dl, f"{paused_dl} -> {again['downloaded']}")

        dst_parent = Path(paused["dst_path"]).parent
        part = Path(paused["dst_path"] + ".part")
        check("断点临时文件保留", part.exists())
        check("目标文件未提前出现", not Path(paused["dst_path"]).exists())

        api("POST", f"/api/tasks/{tid}/resume")
        resumed = wait_task(tid, {"downloading", "pending"}, downloaded_gt=paused_dl)
        check("恢复后从断点继续", True, f"{paused_dl} -> {resumed['downloaded']}")

        api("POST", f"/api/tasks/{tid}/cancel")
        canceled = wait_task(tid, {"canceled"})
        canceled_dl = canceled["downloaded"]
        check("取消后状态为 canceled", True, f"{canceled_dl} 字节")
        check("取消后目标文件未生成", not Path(canceled["dst_path"]).exists())

        # 关键顺序：此时任务仍是 canceled，先做状态校验，不要先 retry。
        check("校验前状态仍为 canceled", get_task(tid)["status"] == "canceled")
        expect_error("POST", f"/api/tasks/{tid}/pause", "不可暂停")
        expect_error("POST", f"/api/tasks/{tid}/cancel", "无需取消")
        expect_error("POST", f"/api/tasks/{tid}/resume", "未暂停")

        api("POST", f"/api/tasks/{tid}/retry")
        retried = wait_task(tid, {"downloading", "pending"}, downloaded_gt=canceled_dl)
        check("取消后重试可从断点续传", True, f"{canceled_dl} -> {retried['downloaded']}")

        api("POST", f"/api/tasks/{tid}/cancel")
        wait_task(tid, {"canceled"})
        check("重试后的任务可再次取消", True)
    except Exception as exc:  # noqa: BLE001 - 测试脚本需要汇总失败而不是直接崩溃
        check("执行过程无异常", False, str(exc))
    finally:
        if tid is not None:
            try:
                client.request("DELETE", f"/api/tasks/{tid}")
            except Exception:  # noqa: BLE001
                pass
        try:
            put_settings(**original)
        except Exception:  # noqa: BLE001
            pass
        if dst_parent is not None:
            shutil.rmtree(dst_parent, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n结果：{passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
