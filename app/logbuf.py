from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime
from typing import Any

# 诊断导出时的脱敏规则：只保留 Cookie 名，值一律打码。
_SECRET_PAIR = re.compile(
    r"(?i)(__puus|__pus|__kps|__kp|__uid|p_uid|p_ph|_puus|_pus|cookie|authorization)"
    r"(\s*[:=]\s*)([^;\s,]+)"
)

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def redact(text: str) -> str:
    return _SECRET_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text or "")


class RingBufferHandler(logging.Handler):
    """把日志留在内存里，供日志页与诊断导出使用。"""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
            item = {
                "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": redact(message),
            }
        except Exception:  # noqa: BLE001 - 日志处理器自身不能抛异常
            return
        with self._lock:
            self._items.append(item)

    def tail(self, level: str = "", keyword: str = "", limit: int = 200) -> list[dict[str, Any]]:
        level = (level or "").upper()
        keyword = (keyword or "").lower()
        with self._lock:
            items = list(self._items)
        result = []
        for item in reversed(items):
            if level and item["level"] != level:
                continue
            if keyword and keyword not in item["message"].lower() and keyword not in item["logger"].lower():
                continue
            result.append(item)
            if len(result) >= limit:
                break
        return result


LOG_BUFFER = RingBufferHandler()


def install(level: str = "INFO") -> None:
    handler = LOG_BUFFER
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    if handler not in root.handlers:
        root.addHandler(handler)
