from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cookie      TEXT    NOT NULL,
    nickname    TEXT,
    updated_at  TEXT    NOT NULL,
    valid       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS task (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fid         TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    src_path    TEXT    NOT NULL DEFAULT '',
    dst_path    TEXT    NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',
    error       TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    segments_json TEXT,
    priority    INTEGER NOT NULL DEFAULT 0,
    batch_id    TEXT,
    batch_name  TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_status ON task (status);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS speed_sample (
    ts    INTEGER PRIMARY KEY,
    speed REAL    NOT NULL
);
"""


class DB:
    """极简 aiosqlite 封装：单连接 + 写锁，足够单用户 NAS 场景使用。"""

    def __init__(self, path: Path, duckdb_path: Path | None = None) -> None:
        self.path = path
        self.duckdb_path = duckdb_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._migrate()
        await self._import_duckdb()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """给旧库补齐新增列，避免升级后直接报错。"""
        cur = await self._conn.execute("PRAGMA table_info(task)")
        columns = {row[1] for row in await cur.fetchall()}
        await cur.close()
        for name, sql_type in (
            ("batch_id", "TEXT"),
            ("batch_name", "TEXT"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ("segments_json", "TEXT"),
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                await self._conn.execute(f"ALTER TABLE task ADD COLUMN {name} {sql_type}")

    async def _import_duckdb(self) -> None:
        """若曾用过 DuckDB 版本，首次启动把数据搬回 SQLite 并备份原文件。"""
        legacy = self.duckdb_path
        if legacy is None or not legacy.exists():
            return
        try:
            import duckdb
        except ImportError:
            log.warning(
                "检测到旧版 DuckDB 数据库 %s，但未安装 duckdb，无法自动迁移；"
                "如需迁移请先 pip install duckdb",
                legacy,
            )
            return
        try:
            source = duckdb.connect(str(legacy))
        except Exception as exc:  # noqa: BLE001 - 迁移失败不影响新库使用
            log.warning("旧版 DuckDB 数据库无法打开，跳过迁移：%s", exc)
            return
        try:
            for table in ("account", "task", "setting", "speed_sample"):
                try:
                    rows = source.execute(f"SELECT * FROM {table}").fetchall()
                    names = [item[0] for item in source.description]
                except Exception:  # noqa: BLE001 - 旧库没有该表
                    continue
                if not rows:
                    continue
                cur = await self._conn.execute(f"SELECT COUNT(*) FROM {table}")
                filled = (await cur.fetchone())[0]
                await cur.close()
                if filled:
                    continue
                marks = ", ".join("?" for _ in names)
                await self._conn.executemany(
                    f'INSERT INTO {table} ({", ".join(names)}) VALUES ({marks})',
                    [tuple(row) for row in rows],
                )
        finally:
            source.close()
        backup = legacy.with_name(f"{legacy.name}.bak")
        try:
            legacy.replace(backup)
            log.info("旧版 DuckDB 数据已迁回 %s，原文件备份为 %s", self.path.name, backup.name)
        except OSError as exc:
            log.warning("DuckDB 旧库已迁移，但备份重命名失败：%s", exc)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not opened")
        return self._conn

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur.lastrowid or 0

    async def executemany(self, sql: str, params: Iterable[Iterable[Any]]) -> None:
        async with self._lock:
            await self.conn.executemany(sql, [tuple(p) for p in params])
            await self.conn.commit()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, tuple(params))
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, tuple(params))
        row = await cur.fetchone()
        await cur.close()
        return row
