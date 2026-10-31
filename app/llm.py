"""可选的 LLM 兜底：看不懂的文件名交给模型，换回 TMDB 能检索的片名。

模型只产出候选片名，是否采用仍由 TMDB 精确命中 + 年份窗口裁决；
没配地址/Key/模型、超时、返回不是合法 JSON 时一律返回 None，整理流程照旧。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# 只让模型做「识别作品」这一件事，并明确禁止编造，免得把条目带到不存在的片名上。
_SYSTEM_PROMPT = """你是影视文件名解析器。用户会给出一个网盘文件名，它可能被截断，或混有字幕组、画质、音轨、编码等发布标记，也可能是中英文混写。

你的任务是判断这个文件名指的是哪部影视作品，并给出 TMDB 上可以检索到的片名。

要求：
1. 只输出一个 JSON 对象，不要解释、不要 Markdown、不要代码块标记。
2. title 填官方中文译名；没有可靠中文译名时填外文原名或日文原名。
3. alt_titles 填 1-3 个外文原名、英文名，按可能性从高到低；没有就给空数组。
4. year 填首映年份（整数），无法确定填 null。
5. kind 只能是 "movie" 或 "episode"。
6. 判断不出具体作品时，title 填 null，绝不能编造不存在的作品。
7. 不要把字幕组名、分辨率、音轨、编码、文件扩展名写进 title。

输出示例：
{"title": "哈尔的移动城堡", "alt_titles": ["Howl's Moving Castle"], "year": 2004, "kind": "movie"}"""


def _json_block(content: str) -> str:
    """模型偶尔会带 ```json 包裹或前后废话，取第一个到最后一个花括号。"""
    start = content.find("{")
    end = content.rfind("}")
    return content[start : end + 1] if 0 <= start < end else ""


def _clean_title(value: Any) -> str:
    text = str(value or "").strip()
    return text if text and len(text) <= 200 else ""


class LlmClient:
    """OpenAI 兼容的 /chat/completions 客户端，只用来把文件名解析成候选片名。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def suggest(
        self, raw_name: str, kind: str, year: int | None
    ) -> dict[str, Any] | None:
        """把原始文件名交给模型，返回 {title, alt_titles, year, kind}，失败返回 None。"""
        raw_name = (raw_name or "").strip()
        if not raw_name:
            return None
        user = (
            f"文件名：{raw_name}\n"
            f"已知类型：{'剧集' if kind == 'episode' else '电影'}\n"
            f"已知年份：{year if year else '未知'}"
        )
        try:
            resp = await self._http.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("LLM 解析失败：%s", exc)
            return None

        try:
            data = json.loads(_json_block(str(content or "")))
        except ValueError:
            log.warning("LLM 返回不是合法 JSON：%r", str(content)[:200])
            return None
        if not isinstance(data, dict):
            return None

        title = _clean_title(data.get("title"))
        if not title:
            return None
        alts: list[str] = []
        raw_alts = data.get("alt_titles")
        if isinstance(raw_alts, list):
            for item in raw_alts:
                alt = _clean_title(item)
                if alt and alt != title and alt not in alts:
                    alts.append(alt)
        parsed_year = data.get("year")
        if not isinstance(parsed_year, int) or isinstance(parsed_year, bool):
            parsed_year = None
        parsed_kind = data.get("kind")
        return {
            "title": title,
            "alt_titles": alts[:3],
            "year": parsed_year,
            "kind": parsed_kind if parsed_kind in ("movie", "episode") else kind,
        }
