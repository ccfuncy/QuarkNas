from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from ..db import DB
from ..settings import Settings
from .client import CookieExpired, QuarkClient, QuarkError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_cookie(raw: str) -> str:
    """支持两种粘贴格式：浏览器 Cookie 字符串，或扩展导出的 JSON 对象。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Cookie 不能为空")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Cookie JSON 解析失败") from exc
        pairs = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name"):
                    pairs.append(f"{item['name']}={item.get('value', '')}")
        elif isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict):
                    value = value.get("value", "")
                pairs.append(f"{key}={value}")
        if not pairs:
            raise ValueError("Cookie JSON 中没有可用字段")
        text = "; ".join(pairs)
    if "=" not in text:
        raise ValueError("Cookie 格式不正确，应形如 a=b; c=d")
    return text


class AccountStore:
    def __init__(self, db: DB, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._client: QuarkClient | None = None

    async def get_cookie(self) -> str | None:
        row = await self.db.fetchone("SELECT cookie FROM account WHERE id = 1")
        return row["cookie"] if row else None

    async def status(self) -> dict:
        row = await self.db.fetchone(
            "SELECT cookie, nickname, updated_at, valid FROM account WHERE id = 1"
        )
        if not row:
            return {"configured": False, "valid": False, "nickname": "", "updated_at": None}
        return {
            "configured": True,
            "valid": bool(row["valid"]),
            "nickname": row["nickname"] or "",
            "updated_at": row["updated_at"],
        }

    async def save(self, raw_cookie: str) -> dict:
        cookie = normalize_cookie(raw_cookie)
        probe = QuarkClient(cookie, self.settings)
        try:
            info = await probe.account_info()
            # 补齐 CDN 二次鉴权需要的 __puus，否则下载直链一律 412。
            cookie = await probe.refresh_session()
        finally:
            await probe.close()
        await self.db.execute(
            """
            INSERT INTO account (id, cookie, nickname, updated_at, valid)
            VALUES (1, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                cookie = excluded.cookie,
                nickname = excluded.nickname,
                updated_at = excluded.updated_at,
                valid = 1
            """,
            (cookie, info.get("nickname") or "", _now()),
        )
        try:
            os.chmod(self.db.path, 0o600)
        except OSError:
            pass
        await self.reset_client()
        return await self.status()

    async def mark_invalid(self) -> None:
        await self.db.execute("UPDATE account SET valid = 0 WHERE id = 1")
        await self.reset_client()

    async def client(self) -> QuarkClient:
        if self._client is not None:
            return self._client
        cookie = await self.get_cookie()
        if not cookie:
            raise CookieExpired(31001, "尚未配置夸克 Cookie")
        self._client = QuarkClient(cookie, self.settings)
        return self._client

    async def reset_client(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def check(self) -> dict:
        try:
            client = await self.client()
            info = await client.account_info()
        except CookieExpired:
            await self.mark_invalid()
            return {"valid": False, "nickname": "", "message": "Cookie 已失效，请重新粘贴"}
        except QuarkError as exc:
            return {"valid": False, "nickname": "", "message": exc.message}
        nickname = (info.get("nickname") or "").strip()
        # 顺带刷新昵称：数据库里的可能是登录时抓到的旧昵称，会和实时结果不一致。
        if nickname:
            await self.db.execute(
                "UPDATE account SET valid = 1, nickname = ? WHERE id = 1", (nickname,)
            )
        else:
            await self.db.execute("UPDATE account SET valid = 1 WHERE id = 1")
        latest = client.cookie
        try:
            latest = await client.refresh_session()
        except Exception:  # noqa: BLE001 - 补 Cookie 失败不影响本次校验结果
            pass
        if latest != await self.get_cookie():
            await self.db.execute("UPDATE account SET cookie = ? WHERE id = 1", (latest,))
        return {"valid": True, "nickname": info.get("nickname", ""), "message": "Cookie 有效"}
