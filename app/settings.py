from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 夸克网页版请求需要固定的 UA 特征，直链与身份/UA 绑定，下载时必须沿用同一个 UA。
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
    "quark-cloud-drive/3.14.2 Channel/pckk_other_ch"
)

# 影视整理可选的 TMDB 标题语言；选中文时缺中文名会回退到中文别名。
TMDB_LANGUAGES = ("zh-CN", "zh-TW", "en-US")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = _env(name, default)
    return value if value in choices else default


@dataclass(frozen=True)
class Settings:
    download_root: Path
    config_dir: Path
    host: str
    port: int
    segments: int
    concurrency: int
    low_watermark_gb: float
    auth_password: str
    user_agent: str
    request_timeout: float
    retries: int
    tmdb_language: str

    @property
    def db_path(self) -> Path:
        return self.config_dir / "quarknas.db"

    @property
    def duckdb_path(self) -> Path:
        """旧版 DuckDB 数据库，首次启动时会被迁回 SQLite 并改名备份。"""
        return self.config_dir / "quarknas.duckdb"

    def ensure_dirs(self) -> None:
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings(
        download_root=Path(_env("DOWNLOAD_ROOT", "/downloads")).expanduser(),
        config_dir=Path(_env("CONFIG_DIR", "/config")).expanduser(),
        host=_env("HOST", "0.0.0.0"),
        port=_int("PORT", 8080),
        segments=max(1, min(16, _int("SEGMENTS", 4))),
        concurrency=max(1, min(8, _int("CONCURRENCY", 2))),
        low_watermark_gb=max(0.0, _float("LOW_WATERMARK_GB", 2.0)),
        auth_password=_env("AUTH_PASSWORD", ""),
        user_agent=_env("QUARK_USER_AGENT", DEFAULT_UA),
        request_timeout=max(5.0, _float("REQUEST_TIMEOUT", 30.0)),
        retries=max(1, _int("RETRIES", 3)),
        tmdb_language=_choice("TMDB_LANGUAGE", "zh-CN", TMDB_LANGUAGES),
    )
