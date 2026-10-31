from __future__ import annotations

import re
from pathlib import Path

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Linux 的 NAME_MAX 是 255 字节（ext4/xfs），中文一字 3 字节，
# 按字符数截到 200 在容器里仍会写失败，所以这里按 UTF-8 字节数收。
_MAX_COMPONENT = 200


def _clip(name: str, limit: int) -> str:
    """按 UTF-8 字节数截断，且不把多字节字符切一半。"""
    raw = name.encode("utf-8")
    if len(raw) <= limit:
        return name
    return raw[:limit].decode("utf-8", "ignore")


def safe_component(name: str) -> str:
    """把夸克里的文件名转成 NAS 上安全的单层路径名。"""
    name = (name or "").strip().replace("\u200b", "")
    name = _ILLEGAL.sub("_", name)
    name = name.strip().rstrip(". ")
    if not name:
        name = "_"
    if len(name.encode("utf-8")) > _MAX_COMPONENT:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 16:
            keep = _MAX_COMPONENT - len(ext.encode("utf-8")) - 1
            name = f"{_clip(stem, max(keep, 1))}.{ext}"
        else:
            name = _clip(name, _MAX_COMPONENT)
    return name


def safe_relpath(parts: list[str]) -> Path:
    cleaned = [safe_component(p) for p in parts if p and p.strip()]
    return Path(*cleaned) if cleaned else Path()


def resolve_target(root: Path, parts: list[str]) -> Path:
    """把相对路径拼到下载根目录下，并确保不会逃逸出去。"""
    target = (root / safe_relpath(parts)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"非法目标路径: {target}")
    return target
