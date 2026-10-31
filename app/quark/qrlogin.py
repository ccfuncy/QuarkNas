from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

import httpx
import qrcode
import qrcode.image.svg

from ..settings import DEFAULT_UA
from .client import QuarkError

log = logging.getLogger("quarknas.qr")

TOKEN_URL = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
TICKET_URL = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
INFO_URL = "https://pan.quark.cn/account/info"
QR_BASE = "https://su.quark.cn/4_eMHBJ"
CLIENT_ID = "532"
SESSION_TTL = 180.0

# 能代表登录态的 Cookie 字段。新版网页会话给的是 __pus/__kps，老版本给 __puus。
SESSION_COOKIES = ("__puus", "__pus", "__kps", "__uid")

HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://pan.quark.cn/",
}


def _has_session(jar) -> bool:
    return any(cookie.name in SESSION_COOKIES for cookie in jar)


@dataclass
class QrSession:
    token: str
    client: httpx.AsyncClient
    created: float
    status: str = "waiting"
    nickname: str = ""
    cookie: str = ""
    message: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def content(self) -> str:
        """二维码里真正编码的地址，夸克 App 扫码后跳转到这里完成授权。"""
        import urllib.parse

        query = urllib.parse.urlencode(
            {
                "token": self.token,
                "client_id": CLIENT_ID,
                "ssb": "weblogin",
                "uc_param_str": "",
                "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0",
            }
        )
        return f"{QR_BASE}?{query}"

    def svg(self) -> bytes:
        image = qrcode.make(
            self.content,
            image_factory=qrcode.image.svg.SvgPathImage,
            box_size=10,
            border=2,
        )
        return image.to_string()


class QrLoginManager:
    """扫码登录：生成二维码 → 轮询扫码结果 → 用 service_ticket 换 Cookie。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._sessions: dict[str, QrSession] = {}

    async def _gc(self) -> None:
        now = time.monotonic()
        for sid in list(self._sessions):
            session = self._sessions[sid]
            expired = now - session.created > SESSION_TTL
            finished = session.status in ("confirmed", "failed", "expired")
            if expired or (finished and now - session.created > 5):
                await session.client.aclose()
                if expired and session.status == "waiting":
                    session.status = "expired"
                else:
                    self._sessions.pop(sid, None)
        for sid in list(self._sessions):
            if self._sessions[sid].status == "expired":
                self._sessions.pop(sid, None)

    async def create(self) -> dict:
        await self._gc()
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.request_timeout, connect=10.0),
            follow_redirects=True,
            headers=HEADERS,
        )
        try:
            resp = await client.get(
                TOKEN_URL,
                params={"client_id": CLIENT_ID, "v": "1.2", "request_id": str(uuid.uuid4())},
            )
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            await client.aclose()
            raise QuarkError(-1, f"获取二维码失败：{exc}") from exc

        if payload.get("status") != 2000000:
            await client.aclose()
            raise QuarkError(payload.get("status") or -1, payload.get("message") or "获取二维码失败")

        token = (payload.get("data") or {}).get("members", {}).get("token")
        if not token:
            await client.aclose()
            raise QuarkError(-1, "二维码响应中缺少 token")

        sid = uuid.uuid4().hex
        self._sessions[sid] = QrSession(token=token, client=client, created=time.monotonic())
        return {"sid": sid, "expires_in": int(SESSION_TTL)}

    def image(self, sid: str) -> bytes | None:
        session = self._sessions.get(sid)
        return session.svg() if session else None

    async def poll(self, sid: str) -> dict:
        await self._gc()
        session = self._sessions.get(sid)
        if session is None:
            return {"status": "expired", "message": "二维码已失效，请刷新"}

        if session.status != "waiting":
            return {
                "status": session.status,
                "nickname": session.nickname,
                "message": session.message,
                "cookie": session.cookie,
            }

        if time.monotonic() - session.created > SESSION_TTL:
            session.status = "expired"
            session.message = "二维码已过期，请刷新"
            return {"status": "expired", "message": session.message}

        try:
            resp = await session.client.get(
                TICKET_URL,
                params={
                    "client_id": CLIENT_ID,
                    "v": "1.2",
                    "token": session.token,
                    "request_id": str(uuid.uuid4()),
                },
            )
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "waiting", "message": f"轮询异常：{exc}"}

        status = payload.get("status")
        message = str(payload.get("message") or "")
        ticket = ((payload.get("data") or {}).get("members") or {}).get("service_ticket")
        log.info("扫码轮询：status=%s message=%s ticket=%s", status, message, "有" if ticket else "无")

        if status == 2000000 and ticket:
            cookie = await self._exchange(session, ticket)
            log.info("票据换取 Cookie：%d 个字段", cookie.count(";") + 1 if cookie else 0)
            if cookie:
                session.status = "confirmed"
                session.cookie = cookie
                return {
                    "status": "confirmed",
                    "nickname": session.nickname,
                    "cookie": cookie,
                    "message": "扫码成功",
                }
            session.status = "failed"
            session.message = "已扫码，但换取 Cookie 失败，请重试"
            return {"status": "failed", "message": session.message}

        # 50004001 / "Query result is empty" 表示还没扫码，属于正常等待态。
        if status in (50004001, 50004002) or "empty" in message.lower():
            return {"status": "waiting", "message": "等待扫码…"}

        session.status = "failed"
        session.message = message or "扫码登录失败"
        return {"status": "failed", "message": session.message}

    async def close(self, sid: str) -> None:
        session = self._sessions.pop(sid, None)
        if session is not None:
            await session.client.aclose()

    async def _exchange(self, session: QrSession, ticket: str) -> str:
        """用 service_ticket 调账号接口，Cookie 会落进同一个 client 的 cookie jar。"""
        try:
            resp = await session.client.get(INFO_URL, params={"st": ticket, "lw": "scan"})
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("票据换取 Cookie 异常：%s", exc)
            return ""
        data = payload.get("data") or {}
        if data.get("nickname") or data.get("nick_name"):
            session.nickname = data.get("nickname") or data.get("nick_name") or ""
        jar = [c for c in session.client.cookies.jar if c.domain and "quark.cn" in c.domain]
        log.info(
            "账号接口 http=%s code=%s，cookie 字段=%s",
            resp.status_code,
            payload.get("status"),
            [c.name for c in jar],
        )
        if not _has_session(jar):
            log.info("响应体片段：%s", resp.text[:200].replace("\n", " "))
            # 有些会话只在访问网盘首页后才补发登录 Cookie，这里再走一次。
            try:
                await session.client.get("https://pan.quark.cn/")
            except httpx.HTTPError:
                pass
            jar = [c for c in session.client.cookies.jar if c.domain and "quark.cn" in c.domain]
            log.info("二次采集 cookie 字段=%s", [c.name for c in jar])
            if not _has_session(jar):
                return ""
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)
