"""云端影视整理：扫描 → 元数据校正 → 预演 → 执行。

扫描只读：遍历指定目录，用本地规则拆片名/年份/季集，先把「原路径 → 目标路径」
返回给前端预演；前端再分批调用 `/enrich` 校正片名和年份。校正只用 TMDB（需要
在设置里填 API Key，没填就不做校正）。执行阶段才真正建目录、改名、移动。
目标结构固定为 Jellyfin/Emby 习惯的 `电影/片名 (年份)/` 与
`电视剧/剧名 (年份)/Season 01/`，均建在网盘根目录下。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..media import (
    LIBRARY_DIRS,
    SIDECAR_EXTS,
    VIDEO_EXTS,
    TmdbClient,
    TmdbError,
    parse_aliases,
    parse_media,
    pick_folder,
    target_paths,
)
from ..paths import safe_component
from ..quark.client import QuarkError
from ..runtime import Ctx, get_ctx
from .files import quark_http_error

router = APIRouter(prefix="/api/organize", tags=["organize"])

ROOT_FID = "0"
# 遍历上限只用来防草稿目录这类异常结构，正常影视目录远达不到。
MAX_WALK_ITEMS = 20000
MAX_DEPTH = 8
MAX_APPLY = 300
MAX_CHECK_DIRS = 80
# 单次校正请求最多处理这么多条目，真正发出去的查询限制到 20 条，免得一个请求过久。
MAX_ENRICH = 60
MAX_TMDB_QUERIES = 20

# TMDB 客户端随 Key / 节点列表地址 / 标题语言 / 片名别名变化重建，改了设置立刻生效。
_tmdb: tuple[str, str, str, str, TmdbClient] | None = None


def _tmdb_client(
    api_key: str, hosts_url: str, language: str, aliases_text: str
) -> TmdbClient:
    global _tmdb
    key = api_key.strip()
    hosts = hosts_url.strip()
    lang = language.strip() or "zh-CN"
    aliases = aliases_text.strip()
    if (
        _tmdb is None
        or _tmdb[0] != key
        or _tmdb[1] != hosts
        or _tmdb[2] != lang
        or _tmdb[3] != aliases
    ):
        _tmdb = (
            key,
            hosts,
            lang,
            aliases,
            TmdbClient(key, hosts, lang, parse_aliases(aliases)),
        )
    return _tmdb[4]


# 预告、样片、花絮这类文件不该进影视库。
_SKIP_WORDS = ("sample", "trailer", "预告", "花絮", "特典", "样片")


def _split_ext(name: str) -> tuple[str, str]:
    ext = Path(name).suffix.lower()
    if ext:
        return name[: -len(ext)], ext
    return name, ""


def _skip_video(name: str) -> bool:
    low = name.lower()
    return any(word in low for word in _SKIP_WORDS)


def _is_library(parts: list[str]) -> bool:
    return bool(parts) and parts[0] in LIBRARY_DIRS


def _suffix_label(text: str) -> str:
    """字幕文件里片名之后的那一截，如「.chs」「.zh-CN」，拼到目标名后面。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", ".", text or "").strip(".")
    if not cleaned or len(cleaned) > 12:
        return ""
    return "." + cleaned


class FolderResolver:
    """按路径逐层查找/创建云端目录，并缓存每一层的子目录清单。"""

    def __init__(self, client: Any) -> None:
        self.client = client
        # 父目录 fid → {子目录名: 子目录 fid}
        self._cache: dict[str, dict[str, str]] = {}

    async def children(self, fid: str) -> dict[str, str]:
        cached = self._cache.get(fid)
        if cached is not None:
            return cached
        names: dict[str, str] = {}
        page = 1
        while True:
            result = await self.client.list_files(pdir_fid=fid, page=page, size=200)
            for entry in result["entries"]:
                if entry["dir"]:
                    names[entry["name"]] = entry["fid"]
            if page * 200 >= result["total"] or not result["entries"]:
                break
            page += 1
        self._cache[fid] = names
        return names

    async def resolve(self, parts: list[str]) -> str | None:
        fid = ROOT_FID
        for part in parts:
            child = (await self.children(fid)).get(part)
            if not child:
                return None
            fid = child
        return fid

    async def ensure(self, parts: list[str]) -> str:
        fid = ROOT_FID
        for part in parts:
            names = await self.children(fid)
            child = names.get(part)
            if not child:
                child = await self.client.create_folder(fid, part)
                names[part] = child
            fid = child
        return fid


async def _collect(
    client: Any,
    fid: str,
    parts: list[str],
    out: list[tuple[dict, str, list[str]]],
    budget: dict[str, Any],
) -> None:
    """递归收集视频与字幕，记录父目录 fid；已在影视库目录下的子树直接跳过。"""
    if _is_library(parts):
        return
    async for entry in client.iter_children(fid):
        budget["n"] += 1
        if budget["n"] > MAX_WALK_ITEMS:
            budget["truncated"] = True
            return
        name = entry["name"]
        if entry["dir"]:
            if len(parts) >= MAX_DEPTH:
                continue
            await _collect(client, entry["fid"], parts + [name], out, budget)
            if budget["truncated"]:
                return
            continue
        ext = Path(name).suffix.lower()
        if ext in VIDEO_EXTS:
            if _skip_video(name):
                budget["skipped"] += 1
                continue
            out.append((entry, fid, parts))
            budget["videos"] += 1
        elif ext in SIDECAR_EXTS:
            out.append((entry, fid, parts))


def _sidecar_name(base_stem: str, side_name: str, video_stem: str) -> str:
    """字幕跟着视频改名：沿用视频目标名，只保留字幕自己的语言后缀。"""
    side_stem, side_ext = _split_ext(side_name)
    suffix = ""
    if side_stem.lower().startswith(video_stem.lower()):
        suffix = _suffix_label(side_stem[len(video_stem) :])
    return f"{base_stem}{suffix}{side_ext}"


class ScanRequest(BaseModel):
    fid: str = ROOT_FID
    path: list[str] = Field(default_factory=list)


class EnrichItem(BaseModel):
    fid: str
    title: str
    kind: str = "movie"
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    ext: str = ""
    # 文件名里被切掉的外文原名，检索时用来交叉确认条目。
    alt_titles: list[str] = Field(default_factory=list)


class EnrichRequest(BaseModel):
    items: list[EnrichItem] = Field(default_factory=list)


class SidecarRef(BaseModel):
    fid: str
    name: str
    parent_fid: str = ""
    target_name: str = ""


class ApplyItem(BaseModel):
    fid: str
    name: str
    parent_fid: str = ""
    target_dir: list[str] = Field(default_factory=list)
    target_name: str
    sidecars: list[SidecarRef] = Field(default_factory=list)


class ApplyRequest(BaseModel):
    items: list[ApplyItem] = Field(default_factory=list)


@router.post("/scan")
async def scan(req: ScanRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    base = [part for part in (req.path or []) if part and part.strip()]
    try:
        client = await ctx.account.client()
    except QuarkError as exc:
        raise quark_http_error(exc) from exc

    found: list[tuple[dict, str, list[str]]] = []
    budget: dict[str, Any] = {"n": 0, "videos": 0, "skipped": 0, "truncated": False}
    try:
        await _collect(client, req.fid or ROOT_FID, base, found, budget)
    except QuarkError as exc:
        raise quark_http_error(exc) from exc

    videos = [
        (entry, parent, parts)
        for entry, parent, parts in found
        if Path(entry["name"]).suffix.lower() in VIDEO_EXTS
    ]
    sidecars = [
        (entry, parent, parts)
        for entry, parent, parts in found
        if Path(entry["name"]).suffix.lower() in SIDECAR_EXTS
    ]

    items: list[dict[str, Any]] = []
    attached: set[str] = set()
    for entry, parent, parts in videos:
        stem, ext = _split_ext(entry["name"])
        info = parse_media(entry["name"], pick_folder(parts))
        dirs, target_name = target_paths(info, ext)
        item = {
            "fid": entry["fid"],
            "name": entry["name"],
            "size": entry["size"],
            "parent_fid": parent,
            "source": "local",
            "ext": ext,
            "title": info["title"],
            "alt_titles": info["alt_titles"],
            "year": info["year"],
            "kind": info["kind"],
            "season": info["season"],
            "episode": info["episode"],
            "confidence": info["confidence"],
            "reason": info["reason"],
            "target_parts": dirs,
            "target_name": target_name,
            "sidecars": [],
            "conflict": False,
            "conflict_reason": "",
        }
        for side, side_parent, side_parts in sidecars:
            if side["fid"] in attached or side_parent != parent:
                continue
            if side_parts != parts:
                continue
            side_stem = _split_ext(side["name"])[0]
            if not side_stem.lower().startswith(stem.lower()):
                continue
            attached.add(side["fid"])
            item["sidecars"].append(
                {
                    "fid": side["fid"],
                    "name": side["name"],
                    "parent_fid": side_parent,
                    "target_name": _sidecar_name(
                        _split_ext(target_name)[0], side["name"], stem
                    ),
                }
            )
        items.append(item)

    resolver = FolderResolver(client)
    checked: dict[tuple[str, ...], set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for item in items:
        parts = item["target_parts"]
        dir_key = "/".join(parts)
        item["target_dir"] = dir_key
        key = (dir_key, item["target_name"])
        if key in seen:
            item["conflict"] = True
            item["conflict_reason"] = "与本次扫描的其他条目同名"
            continue
        seen.add(key)
        if len(checked) >= MAX_CHECK_DIRS and tuple(parts) not in checked:
            continue
        if tuple(parts) not in checked:
            fid = await resolver.resolve(parts)
            names = set((await resolver.children(fid)).keys()) if fid else set()
            checked[tuple(parts)] = names
        if item["target_name"] in checked[tuple(parts)]:
            item["conflict"] = True
            item["conflict_reason"] = "目标目录已存在同名文件"

    return {
        "items": items,
        "videos": len(items),
        "sidecars": sum(len(item["sidecars"]) for item in items),
        "skipped": budget["skipped"],
        "truncated": budget["truncated"],
    }


@router.post("/enrich")
async def enrich(req: EnrichRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    """分批用 TMDB 校正片名/年份；每次都实时查询，不用本地缓存。没配 Key 就不校正。"""
    if not req.items:
        return {"updates": [], "done": [], "retry": [], "queried": 0, "more": False}

    api_key = str(ctx.config.get("tmdb_api_key") or "").strip()
    if not api_key:
        return {
            "updates": [],
            "done": [],
            "retry": [],
            "queried": 0,
            "more": False,
            "error": "未配置 TMDB API Key，请先在设置里填写",
        }

    language = str(ctx.config.get("tmdb_language") or "zh-CN")
    client = _tmdb_client(
        api_key,
        str(ctx.config.get("tmdb_hosts_url") or ""),
        language,
        str(ctx.config.get("tmdb_aliases") or ""),
    )
    max_queries = MAX_TMDB_QUERIES
    groups: dict[tuple[str, str], list[EnrichItem]] = {}
    for item in req.items[:MAX_ENRICH]:
        groups.setdefault((item.title.strip(), item.kind), []).append(item)

    updates: list[dict[str, Any]] = []
    # done 里的条目已经有结论（校正过或确认没有条目），前端不必再送。
    done: list[str] = []
    # 这一轮没查到的条目，前端会排到队尾再试一次。
    retry: list[str] = []
    queried = 0
    more = False
    error = ""
    for (title, kind), group in groups.items():
        if queried >= max_queries:
            more = True
            break
        queried += 1
        try:
            found = await client.search(
                title, kind, group[0].year, group[0].alt_titles
            )
        except TmdbError as exc:
            # Key 无效这类错误没法靠重试解决，直接把原因带回前端。
            error = str(exc)
            break
        except Exception:  # 接口失败不影响本地结果
            found = None
        done.extend(item.fid for item in group)
        if not found:
            retry.extend(item.fid for item in group)
            continue
        for item in group:
            info = {
                "title": found.title,
                "year": found.year or item.year,
                "kind": item.kind,
                "season": item.season,
                "episode": item.episode,
            }
            dirs, name = target_paths(info, item.ext)
            updates.append(
                {
                    "fid": item.fid,
                    "title": found.title,
                    "year": info["year"],
                    "source": "tmdb",
                    "match": "tmdb",
                    "target_parts": dirs,
                    "target_name": name,
                }
            )

    return {
        "updates": updates,
        "done": done,
        "retry": retry,
        "queried": queried,
        "more": more,
        "error": error,
    }


@router.post("/apply")
async def apply(req: ApplyRequest, ctx: Ctx = Depends(get_ctx)) -> dict:
    if not req.items:
        raise HTTPException(status_code=400, detail="没有需要整理的文件")
    if len(req.items) > MAX_APPLY:
        raise HTTPException(status_code=400, detail=f"一次最多执行 {MAX_APPLY} 条")

    prepared: list[tuple[ApplyItem, list[str], str]] = []
    for raw in req.items:
        fid = (raw.fid or "").strip()
        if not fid:
            raise HTTPException(status_code=400, detail="缺少文件 fid")
        parts = [safe_component(part) for part in raw.target_dir if part and part.strip()]
        parts = [part for part in parts if part]
        if not parts or parts[0] not in LIBRARY_DIRS:
            raise HTTPException(status_code=400, detail="目标目录必须以 电影 或 电视剧 开头")
        if len(parts) > 3:
            raise HTTPException(status_code=400, detail="目标目录层级过深")
        name = safe_component(raw.target_name or raw.name)
        if not name or name == "_":
            raise HTTPException(status_code=400, detail="目标文件名不能为空")
        prepared.append((raw, parts, name))

    try:
        client = await ctx.account.client()
    except QuarkError as exc:
        raise quark_http_error(exc) from exc

    resolver = FolderResolver(client)
    results: list[dict[str, Any]] = []
    for raw, parts, name in prepared:
        entry: dict[str, Any] = {
            "fid": raw.fid,
            "name": raw.name,
            "target_dir": "/".join(parts),
            "target_name": name,
            "ok": False,
        }
        try:
            dir_fid = await resolver.ensure(parts)
            moved = bool(raw.parent_fid) and raw.parent_fid != dir_fid
            if raw.name != name:
                await client.rename(raw.fid, name)
            if moved:
                await client.move([raw.fid], dir_fid)
            base_stem = _split_ext(name)[0]
            for side in raw.sidecars:
                side_name = safe_component(
                    side.target_name or _sidecar_name(base_stem, side.name, _split_ext(raw.name)[0])
                )
                side_parent = side.parent_fid or raw.parent_fid
                if side.name != side_name:
                    await client.rename(side.fid, side_name)
                if side_parent and side_parent != dir_fid:
                    await client.move([side.fid], dir_fid)
            entry["ok"] = True
        except QuarkError as exc:
            entry["error"] = exc.message
        except Exception as exc:  # 单条失败不影响后续条目
            entry["error"] = str(exc)
        results.append(entry)

    ok = sum(1 for item in results if item["ok"])
    return {"results": results, "ok": ok, "failed": len(results) - ok}
