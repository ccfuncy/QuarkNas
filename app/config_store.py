from __future__ import annotations

import json
import logging
import re
from typing import Any

from .db import DB
from .settings import TMDB_LANGUAGES, Settings

log = logging.getLogger(__name__)

ON_CONFLICT = ("skip", "overwrite", "keep_both")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(ValueError):
    """配置项校验失败。"""


def _defaults(settings: Settings) -> dict[str, Any]:
    return {
        "segments": settings.segments,
        "concurrency": settings.concurrency,
        "on_conflict": "skip",
        "low_watermark_gb": settings.low_watermark_gb,
        "speed_limit_mbps": 0.0,
        "schedule_enabled": False,
        "schedule_start": "23:00",
        "schedule_end": "07:00",
        "notify_browser": False,
        "auto_retry_count": 0,
        "auto_retry_backoff": 30,
        "webhook_url": "",
        "webhook_secret": "",
        "queue_order": "time",
        "cleanup_days": 0,
        # 影视整理用的 TMDB Key；留空则不做片名校准。
        "tmdb_api_key": "",
        # CheckTMDB 维护的可用节点 IP 列表，默认走 gh-proxy 镜像；留空则直连官方域名。
        "tmdb_hosts_url": (
            "https://gh-proxy.com/https://raw.githubusercontent.com/"
            "cnwikee/CheckTMDB/refs/heads/main/Tmdb_host_ipv4"
        ),
        # 影视整理换用的标题语言；缺对应语言的标题时按地区别名回退。
        "tmdb_language": settings.tmdb_language,
    }


def _as_int(key: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{key} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 必须是整数") from exc
    if number < low or number > high:
        raise ConfigError(f"{key} 取值范围 {low}–{high}")
    return number


def _as_float(key: str, value: Any, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{key} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 必须是数字") from exc
    if number < low or number > high:
        raise ConfigError(f"{key} 取值范围 {low}–{high}")
    return number


def _as_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{key} 必须是布尔值")


def _as_clock(key: str, value: Any) -> str:
    text = str(value or "").strip()
    if not _TIME_RE.match(text):
        raise ConfigError(f"{key} 必须是 HH:MM 格式")
    return text


def validate(key: str, value: Any) -> Any:
    if key == "segments":
        return _as_int(key, value, 1, 16)
    if key == "concurrency":
        return _as_int(key, value, 1, 8)
    if key == "on_conflict":
        if value not in ON_CONFLICT:
            raise ConfigError("同名文件策略只能是 skip / overwrite / keep_both")
        return value
    if key == "low_watermark_gb":
        return _as_float(key, value, 0.0, 10240.0)
    if key == "speed_limit_mbps":
        return _as_float(key, value, 0.0, 10240.0)
    if key == "auto_retry_count":
        return _as_int(key, value, 0, 10)
    if key == "auto_retry_backoff":
        return _as_int(key, value, 5, 600)
    if key == "webhook_url":
        text = str(value or "").strip()
        if text and not text.startswith(("http://", "https://")):
            raise ConfigError("webhook_url 必须以 http:// 或 https:// 开头")
        return text
    if key == "webhook_secret":
        text = str(value or "").strip()
        if len(text) > 128:
            raise ConfigError("webhook_secret 不能超过 128 个字符")
        return text
    if key == "queue_order":
        if value not in ("time", "size_desc", "size_asc"):
            raise ConfigError("queue_order 只能是 time / size_desc / size_asc")
        return value
    if key == "cleanup_days":
        return _as_int(key, value, 0, 365)
    if key == "tmdb_api_key":
        text = str(value or "").strip()
        if len(text) > 512:
            raise ConfigError("TMDB API Key 不能超过 512 个字符")
        return text
    if key == "tmdb_hosts_url":
        text = str(value or "").strip()
        if len(text) > 512:
            raise ConfigError("TMDB 节点列表地址不能超过 512 个字符")
        if text and not text.startswith(("http://", "https://")):
            raise ConfigError("TMDB 节点列表地址必须以 http:// 或 https:// 开头")
        return text
    if key == "tmdb_language":
        if value not in TMDB_LANGUAGES:
            raise ConfigError("TMDB 标题语言只能是 zh-CN / zh-TW / en-US")
        return value
    if key == "schedule_enabled" or key == "notify_browser":
        return _as_bool(key, value)
    if key in ("schedule_start", "schedule_end"):
        return _as_clock(key, value)
    raise ConfigError(f"未知配置项：{key}")


class ConfigStore:
    """运行期配置：环境变量只作为初始默认值，之后以数据库中的设置为准。"""

    def __init__(self, db: DB, settings: Settings) -> None:
        self.db = db
        self._values = _defaults(settings)

    @property
    def values(self) -> dict[str, Any]:
        return self._values

    def get(self, key: str) -> Any:
        return self._values[key]

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT key, value FROM setting")
        for row in rows:
            key = row["key"]
            if key not in self._values:
                continue
            try:
                value = json.loads(row["value"])
                self._values[key] = validate(key, value)
            except (ValueError, ConfigError):
                log.warning("忽略非法配置 %s=%r", key, row["value"])

    async def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in patch.items():
            if key not in self._values:
                raise ConfigError(f"未知配置项：{key}")
            cleaned[key] = validate(key, value)
        for key, value in cleaned.items():
            await self.db.execute(
                """
                INSERT INTO setting (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, json.dumps(value)),
            )
        self._values.update(cleaned)
        return dict(self._values)
