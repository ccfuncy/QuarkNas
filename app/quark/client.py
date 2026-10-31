from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from ..settings import DEFAULT_UA, Settings

# 夸克网盘没有公开下载 API，只能复用网页端会话。以下域名按优先级尝试。
DRIVE_HOSTS = ("https://drive-pc.quark.cn", "https://drive-m.quark.cn")
ACCOUNT_URL = "https://pan.quark.cn/account/info"
LIST_PATH = "/1/clouddrive/file/sort"
SEARCH_PATH = "/1/clouddrive/file/search"
DOWNLOAD_PATH = "/1/clouddrive/file/download"
RENAME_PATH = "/1/clouddrive/file/rename"
MOVE_PATH = "/1/clouddrive/file/move"
DELETE_PATH = "/1/clouddrive/file/delete"
FOLDER_PATH = "/1/clouddrive/file"
TASK_PATH = "/1/clouddrive/task"
RECYCLE_LIST_PATH = "/1/clouddrive/file/recycle/list"
RECYCLE_RECOVER_PATH = "/1/clouddrive/file/recycle/recover"
RECYCLE_DELETE_PATH = "/1/clouddrive/file/recycle/remove"

# 夸克 CDN 除了校验直链签名，还会用 __puus 做一次会话二次鉴权；
# 缺这个字段时直链一律返回 412。它只在若干接口的响应头里下发。
SESSION_PATH = "/1/clouddrive/member"
SESSION_COOKIE = "__puus"

# 会话失效时夸克返回的业务码。
AUTH_ERROR_CODES = {31001, 31002, 31003, 401, 403}

# 各接口表示成功的 status 值：drive 系列给 200，通行证给 2000000，部分接口给 0。
SUCCESS_STATUS = {0, 200, 2000000}

# 默认排序：目录在前，同类型按修改时间倒序。
DEFAULT_SORT = "file_type:asc,updated_at:desc"


class QuarkError(Exception):
    def __init__(self, code: int | str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class CookieExpired(QuarkError):
    pass


class QuarkClient:
    def __init__(self, cookie: str, settings: Settings) -> None:
        self.cookie = cookie.strip()
        self.settings = settings
        self._host: str | None = None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
            follow_redirects=True,
            headers=self._base_headers(),
        )

    # ------------------------------------------------------------------ 基础

    def _base_headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie,
            "User-Agent": self.settings.user_agent or DEFAULT_UA,
            "Referer": "https://pan.quark.cn/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://pan.quark.cn",
        }

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ Cookie

    def _cookie_map(self) -> dict[str, str]:
        jar: dict[str, str] = {}
        for part in self.cookie.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            jar[name.strip()] = value.strip()
        return jar

    def _merge_cookies(self, headers) -> bool:
        """把响应里的 Set-Cookie 合并进来，返回是否有变化。"""
        merged = False
        for raw in headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            name = name.strip()
            if not name:
                continue
            jar = self._cookie_map()
            value = rest.split(";")[0].strip()
            if jar.get(name) == value:
                continue
            jar[name] = value
            self.cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
            merged = True
        if merged:
            self._http.headers["Cookie"] = self.cookie
        return merged

    async def refresh_session(self, force: bool = False) -> str:
        """补齐 CDN 鉴权所需的 __puus；返回最新 Cookie。

        扫码登录只拿到 __pus/__kps 等字段，缺少 __puus 时直链会 412。
        """
        if not force and SESSION_COOKIE in self._cookie_map():
            return self.cookie
        resp = await self._http.get(
            DRIVE_HOSTS[0] + SESSION_PATH,
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
        )
        if resp.status_code < 400:
            self._merge_cookies(resp.headers)
        return self.cookie

    @staticmethod
    def _parse(payload: Any) -> dict:
        """把夸克返回的 JSON 统一成 {'data':..., 'code':...} 并抛出业务异常。

        不同接口的响应格式并不统一：有的给 `code: 0`，有的给 `code: "OK"`，
        还有的只给 `status: 2000000`。这里统一归一化成整数 code。
        """
        if not isinstance(payload, dict):
            raise QuarkError(-1, "响应格式异常")

        def _as_int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        raw_code = payload.get("code")
        if isinstance(raw_code, str):
            text = raw_code.strip()
            if text.lower() in ("ok", "success", ""):
                code = 0
            else:
                try:
                    code = int(text)
                except ValueError:
                    code = -1
        elif raw_code is None:
            code = 0
        else:
            code = _as_int(raw_code)

        status = _as_int(payload.get("status"))
        message = str(payload.get("message") or payload.get("msg") or "请求失败")
        if code in AUTH_ERROR_CODES or status in AUTH_ERROR_CODES:
            raise CookieExpired(code or status, message)
        if payload.get("success") is False:
            raise QuarkError(code or status or -1, message)
        if code != 0:
            raise QuarkError(code, message)
        if status not in SUCCESS_STATUS:
            raise QuarkError(status, message)
        return payload

    async def _hosts(self) -> list[str]:
        if self._host:
            return [self._host]
        return list(DRIVE_HOSTS)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        last_error: Exception | None = None
        for host in await self._hosts():
            url = host + path
            try:
                resp = await self._http.request(method, url, **kwargs)
                if resp.status_code >= 500:
                    last_error = QuarkError(resp.status_code, "服务端错误")
                    continue
                payload = self._parse(resp.json())
                self._host = host
                return payload
            except CookieExpired:
                raise
            except (httpx.HTTPError, ValueError, QuarkError) as exc:
                last_error = exc
                continue
        raise last_error or QuarkError(-1, "所有接口域名均不可用")

    # ------------------------------------------------------------------ 接口

    async def account_info(self) -> dict:
        resp = await self._http.get(
            ACCOUNT_URL,
            params={"fr": "pc", "platform": "pc"},
        )
        try:
            payload = self._parse(resp.json())
        except CookieExpired:
            raise
        except QuarkError as exc:
            # 取不到账号信息基本等同于 Cookie 不可用，统一按失效处理。
            raise CookieExpired(exc.code, "Cookie 无效或已过期") from exc
        data = payload.get("data") or {}
        if not (data.get("nickname") or data.get("nick_name") or data.get("user_id")):
            raise CookieExpired(-1, "Cookie 无效或已过期")
        return {
            "nickname": data.get("nickname") or data.get("nick_name") or "",
            "vip": bool(data.get("vip") or data.get("member_type")),
            "total": data.get("total_capacity"),
            "used": data.get("use_capacity"),
        }

    async def capacity(self) -> dict:
        """账号容量：/1/clouddrive/member 返回总容量与已用容量（字节）。"""
        payload = await self._request(
            "GET",
            SESSION_PATH,
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
        )
        data = payload.get("data") or {}

        def _num(value: Any) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        member_type = str(data.get("member_type") or "")
        return {
            "total": _num(data.get("total_capacity")),
            "used": _num(data.get("use_capacity")),
            "vip": member_type not in ("", "NORMAL", "0"),
            # 原始类型（如 SVIP / EXP_SVIP），前端据此展示具体档位
            "member_type": member_type,
            # 会员到期时间（毫秒时间戳）
            "expires_at": _num(data.get("exp_at")),
        }

    @staticmethod
    def _normalize_list(payload: dict) -> dict:
        data = payload.get("data") or {}
        entries = []
        for item in data.get("list") or []:
            entries.append(
                {
                    "fid": item.get("fid") or "",
                    "name": item.get("file_name") or "",
                    "dir": bool(item.get("dir")),
                    "size": int(item.get("size") or 0),
                    "updated_at": item.get("updated_at"),
                }
            )
        total = int((payload.get("metadata") or {}).get("_total") or len(entries))
        return {"entries": entries, "total": total}

    async def list_files(
        self, pdir_fid: str = "0", page: int = 1, size: int = 50, sort: str = DEFAULT_SORT
    ) -> dict:
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "pdir_fid": pdir_fid,
            "_page": page,
            "_size": size,
            "_fetch_total": 1,
            "_sort": sort,
            "uc_param_str": "",
        }
        payload = await self._request("GET", LIST_PATH, params=params)
        result = self._normalize_list(payload)
        result.update({"page": page, "size": size})
        return result

    async def search_files(
        self, keyword: str, page: int = 1, size: int = 50, sort: str = DEFAULT_SORT
    ) -> dict:
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "_keyword": keyword,
            "_page": page,
            "_size": size,
            "_fetch_total": 1,
            "_sort": sort,
            "uc_param_str": "",
        }
        payload = await self._request("GET", SEARCH_PATH, params=params)
        result = self._normalize_list(payload)
        result.update({"page": page, "size": size, "keyword": keyword})
        return result

    async def iter_children(self, pdir_fid: str = "0", page_size: int = 100) -> AsyncIterator[dict]:
        page = 1
        while True:
            result = await self.list_files(pdir_fid=pdir_fid, page=page, size=page_size)
            for entry in result["entries"]:
                yield entry
            if page * page_size >= result["total"] or not result["entries"]:
                break
            page += 1

    async def walk(self, pdir_fid: str = "0", *, max_depth: int = 20, max_items: int = 5000) -> AsyncIterator[tuple[dict, list[str]]]:
        """递归遍历目录，产出 (文件条目, 相对目录名列表)。"""
        counter = {"n": 0}

        async def _walk(fid: str, rel: list[str], depth: int) -> AsyncIterator[tuple[dict, list[str]]]:
            if depth > max_depth:
                return
            async for entry in self.iter_children(fid):
                counter["n"] += 1
                if counter["n"] > max_items:
                    raise QuarkError(-1, f"目录条目超过 {max_items}，请分批选择")
                if entry["dir"]:
                    async for item in _walk(entry["fid"], rel + [entry["name"]], depth + 1):
                        yield item
                else:
                    yield entry, rel

        gen = _walk(pdir_fid, [], 0)
        async for item in gen:
            yield item

    async def dir_size(
        self,
        fid: str,
        *,
        semaphore: asyncio.Semaphore | None = None,
        budget: dict[str, int] | None = None,
        max_depth: int = 8,
    ) -> tuple[int, bool]:
        """递归统计目录占用（字节）。

        夸克列目录接口不返回目录大小，只能逐层累加。budget 用来限制一次请求
        里的总条目数，超限时提前停止并返回 complete=False，避免触发风控。
        """
        sem = semaphore or asyncio.Semaphore(4)
        quota = budget if budget is not None else {"left": 4000}

        async def _measure(node: str, depth: int) -> tuple[int, bool]:
            if depth > max_depth:
                return 0, False
            total = 0
            complete = True
            subdirs: list[str] = []
            page = 1
            while True:
                async with sem:
                    result = await self.list_files(pdir_fid=node, page=page, size=200)
                entries = result["entries"]
                quota["left"] -= len(entries)
                for entry in entries:
                    if entry["dir"]:
                        subdirs.append(entry["fid"])
                    else:
                        total += int(entry.get("size") or 0)
                if page * 200 >= result["total"] or not entries:
                    break
                if quota["left"] <= 0:
                    complete = False
                    break
                page += 1

            # 子目录分批并发，既压住请求数也避免一次创建过多任务。
            for start in range(0, len(subdirs), 8):
                if quota["left"] <= 0:
                    complete = False
                    break
                chunk = subdirs[start : start + 8]
                measured = await asyncio.gather(*(_measure(sub, depth + 1) for sub in chunk))
                for size, ok in measured:
                    total += size
                    complete = complete and ok
            return total, complete

        return await _measure(fid, 0)

    async def rename(self, fid: str, name: str) -> None:
        """重命名文件或文件夹；夸克对两者用的是同一个接口。"""
        try:
            await self._request(
                "POST",
                RENAME_PATH,
                params={"pr": "ucpro", "fr": "pc"},
                content=json.dumps({"fid": fid, "file_name": name}),
            )
        except QuarkError as exc:
            if exc.code == 21001:
                raise QuarkError(exc.code, "文件不存在或已被删除") from exc
            raise

    async def _run_file_task(self, path: str, body: dict) -> None:
        """移动/删除是异步任务：接口先返回 task_id，再轮询到任务完成。

        部分情况下接口会同步完成（没有 task_id），此时直接返回。
        """
        payload = await self._request(
            "POST",
            path,
            params={"pr": "ucpro", "fr": "pc"},
            content=json.dumps(body),
        )
        task_id = str(((payload.get("data") or {}).get("task_id")) or "")
        if task_id:
            await self._wait_task(task_id)

    async def _wait_task(self, task_id: str, timeout: float = 20.0) -> None:
        """轮询异步任务直到完成。

        实测返回里 `finish` 一直为 false，真正的状态在 `status`：2 表示完成
        （同时给出 finished_at），失败时给非 0/1/2 的值。两种标记都兼容。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            payload = await self._request(
                "GET",
                TASK_PATH,
                params={
                    "pr": "ucpro",
                    "fr": "pc",
                    "task_id": task_id,
                    "retry_index": 0,
                    "uc_param_str": "",
                },
            )
            data = payload.get("data") or {}
            if data.get("finish") is True:
                return
            try:
                status = int(data.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            if status == 2:
                return
            if status not in (0, 1):
                raise QuarkError(-1, data.get("message") or "操作失败")
            await asyncio.sleep(0.3)
        # 超时不当作失败：任务通常已在服务端生效，列表刷新即可看到结果。

    async def create_folder(self, pdir_fid: str, name: str) -> str:
        """新建文件夹，返回新目录的 fid。"""
        payload = await self._request(
            "POST",
            FOLDER_PATH,
            params={"pr": "ucpro", "fr": "pc"},
            content=json.dumps(
                {
                    "pdir_fid": pdir_fid,
                    "file_name": name,
                    "dir_path": "",
                    "dir_init_lock": False,
                }
            ),
        )
        return str(((payload.get("data") or {}).get("fid")) or "")

    async def move(self, fids: list[str], to_fid: str) -> None:
        try:
            await self._run_file_task(
                MOVE_PATH,
                {"filelist": list(fids), "to_pdir_fid": to_fid},
            )
        except QuarkError as exc:
            if exc.code == 21001:
                raise QuarkError(exc.code, "文件不存在或已被删除") from exc
            raise

    async def delete(self, fids: list[str]) -> None:
        try:
            await self._run_file_task(
                DELETE_PATH,
                {"filelist": list(fids), "exclude_fids": []},
            )
        except QuarkError as exc:
            if exc.code == 21001:
                raise QuarkError(exc.code, "文件不存在或已被删除") from exc
            raise

    async def list_recycle(self, page: int = 1, size: int = 50) -> dict:
        """回收站列表。

        与普通目录的字段不同：没有 `dir`，用 `file_type == 0` 判断文件夹；
        删除时间是 `move_recycle_at`，`move_deep_recycle_at` 是自动彻底删除的时间点。
        """
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "_page": page,
            "_size": size,
            "_fetch_total": 1,
            "_sort": "deleted_at:desc",
            "uc_param_str": "",
        }
        payload = await self._request("GET", RECYCLE_LIST_PATH, params=params)
        data = payload.get("data") or {}
        entries = [
            {
                "fid": item.get("fid") or "",
                # 还原/彻底删除走 record_id，fid 只用于展示和去重。
                "record_id": item.get("record_id") or "",
                "name": item.get("file_name") or "",
                "dir": bool(item.get("dir")) or int(item.get("file_type") or 0) == 0,
                "size": int(item.get("size") or 0),
                "updated_at": item.get("updated_at"),
                "deleted_at": item.get("move_recycle_at"),
                "expire_at": item.get("move_deep_recycle_at"),
            }
            for item in (data.get("list") or [])
        ]
        return {
            "entries": entries,
            "total": int(data.get("total") or len(entries)),
            "page": page,
            "size": size,
        }

    async def recover(self, records: list[str]) -> None:
        """按记录还原到原位置；目标目录已不存在时夸克会退回到根目录。

        select_mode=2 表示按 record_list 精确选择；0 是空选择，1 是全部。
        """
        await self._run_file_task(
            RECYCLE_RECOVER_PATH, {"select_mode": 2, "record_list": list(records)}
        )

    async def purge(self, records: list[str]) -> None:
        """彻底删除，无法再找回。"""
        await self._run_file_task(
            RECYCLE_DELETE_PATH, {"select_mode": 2, "record_list": list(records)}
        )

    async def clear_recycle(self) -> None:
        """清空回收站：select_mode=1 表示全选。"""
        await self._run_file_task(RECYCLE_DELETE_PATH, {"select_mode": 1})

    async def get_download_url(self, fid: str) -> str:
        # 每次取链都刷新一次会话，避免 __puus 过期后直链再次 412。
        await self.refresh_session(force=True)
        payload = await self._request(
            "POST",
            DOWNLOAD_PATH,
            params={"pr": "ucpro", "fr": "pc"},
            content=json.dumps({"fids": [fid]}),
        )
        data = payload.get("data") or []
        if isinstance(data, dict):
            data = [data]
        for item in data:
            url = item.get("download_url")
            if url:
                return url
        raise QuarkError(-1, "未获取到下载直链（可能触发风控或文件不可下载）")
