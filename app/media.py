"""影视文件名解析与 TMDB 检索。

下载来的文件名千奇百怪，这里先按本地规则拆出片名/年份/季集，再可选地用
TMDB 校验并换成中文官方译名。解析部分只做纯计算，方便单独验证。
"""

from __future__ import annotations

import html
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

try:
    from zhconv import convert as _zh_convert
except ImportError:  # 没装也能跑，只是不做繁简归一
    _zh_convert = None

from .llm import LlmClient
from .paths import safe_component

log = logging.getLogger(__name__)

VIDEO_EXTS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".ts",
    ".m2ts",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".rmvb",
    ".rm",
    ".mpg",
    ".mpeg",
    ".vob",
    ".iso",
    ".m4v",
    ".3gp",
}
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}

# 已经在影视库目录里的文件不再重复整理，避免把结构越套越深。
LIBRARY_DIRS = ("电影", "电视剧")
MOVIE_DIR = "电影"
TV_DIR = "电视剧"

# 画质/编码/音轨等标记，出现在标题后面，切掉。
_QUALITY_RE = re.compile(
    r"(?<![a-z0-9])("
    r"2160p|1080p|1080i|720p|480p|540p|4k|8k|uhd|hdr10\+?|hdr|dovi|dolby\.?vision|dv|"
    r"[xh]\.?26[45]|hevc|avc|av1|10bit|8bit|10bits|"
    r"web-?dl|webrip|bluray|blu-ray|bdrip|bd|remux|hdtv|dvdrip|hdrip|"
    r"aac|ac3|eac3|ddp?\s?[257]\.?[01]|dd5\.1|dts(?:-hd)?|truehd|atmos|flac|"
    r"repack|proper|internal|remastered|remaster|60fps|hq"
    r")(?![a-z0-9])",
    re.I,
)

# 常见的中文附加标签，同样不属于片名。
_CN_JUNK_RE = re.compile(
    r"(国语|國語|粤语|粵語|国粤|國粵|日语|日語|英语|英語|韩语|韓語|中文|"
    r"中配|国配|國配|粤配|粵配|台配|中字|中英|中简|中繁|双语|雙語|双字|雙字|"
    r"简繁|简体|繁體|繁体|简中|繁中|内封|内嵌|外挂|外掛|字幕|高清|超清|"
    r"蓝光|藍光|未删减|导演剪辑版|修复版|完整版|合集|合辑|合輯|无水印|無水印|"
    r"高清版|重制版|加长版|剧场版|劇場版)"
)

# 「国日粤三音轨」「國粵日英法韓德音軌」这类多语言音轨标签，整段都不算片名。
_VOICE_TAG_RE = re.compile(
    r"[国國粤粵日英法韓韩德俄西意泰越葡印阿双雙单單全两兩三四五六七八九十\d]{0,12}\s*音[轨軌]"
)

# 中文片名后面跟的外文原名只是别名，如「龙猫 My Neighbor Totoro」「你的名字。Your Name」，
# 不去掉的话跟 TMDB 的中文标题对不上。日文假名也算片名的一部分。
_FOREIGN_TAIL_RE = re.compile(
    r"(?:(?<=[\u3040-\u30ff\u3400-\u9fff])\s+"
    r"|(?<=[\u3400-\u9fff][。！？、，：])\s*)"
    r"[A-Za-z][A-Za-z0-9'’&.,:!?+\- ]*$"
)

# 反过来，外文原名写在前面、中文/日文名写在后面，如「The Wind Rises 風立ちぬ」。
_FOREIGN_HEAD_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9'’&.,:!?+\- ]*?(?=\s+[\u3040-\u30ff\u3400-\u9fff])"
)

# 方括号里出现这些词时是字幕组/发布信息，不能当片名。
_GROUP_TAG_RE = re.compile(
    r"(字幕|壓制|压制|發布|发布|制作|製作|合成|調軸|调轴|搬運|搬运|論壇|论坛|"
    r"社區|社区|官網|官网|下載|下载|資源|资源|電影天堂|电影天堂|磁力|影視|影视|"
    r"www|\.(?:com|net|org|cc|me|top))",
    re.I,
)
_HEAD_BRACKET_RE = re.compile(r"[\[【]([^\]】]{1,60})[\]】]")

_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
# `1920x1040`、`1920×1080` 之类是分辨率，前后数字不该被当成片名年份。
_RESOLUTION_TAIL_RE = re.compile(r"\s*[xX×*]\s*\d{2,5}")
_RESOLUTION_HEAD_RE = re.compile(r"\d{2,5}\s*[xX×*]\s*$")
_RESOLUTION_RE = re.compile(r"(?<![a-z0-9])\d{2,5}\s*[xX×*]\s*\d{2,5}(?![a-z0-9])", re.I)

# 片名里出现这些词时不能当片名用（多半是分类目录）。
_GENERIC_TITLES = {"电影", "电视剧", "剧集", "影片", "下载", "download", "downloads", "movies", "tv", "video", "其他"}

_SEASON_DIR_RE = re.compile(
    r"^(?:season[\s._-]*\d{1,2}|s\d{1,2}|第\s*[一二三四五六七八九十\d]{1,2}\s*季|\d{1,2})$",
    re.I,
)

_CN_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CN_NUMBER_RE = re.compile(r"(?:第\s*)?([一二三四五六七八九十]{1,2})\s*季")


def _cn_number(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_NUMBERS.get(text[1:], 0)
    if text.endswith("十"):
        return _CN_NUMBERS.get(text[:-1], 0) * 10
    if "十" in text:
        head, _, tail = text.partition("十")
        return _CN_NUMBERS.get(head, 0) * 10 + _CN_NUMBERS.get(tail, 0)
    return _CN_NUMBERS.get(text)


def _find_markers(text: str) -> tuple[dict[str, int], int]:
    """找出季/集标记，返回 (值, 最早出现的位置)。

    位置用于切分片名；识别到的数值用于生成目标名。
    """
    found: dict[str, int] = {}
    cut = len(text)

    def take(pos: int) -> None:
        nonlocal cut
        cut = min(cut, pos)

    m = re.search(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,4})", text)
    if m:
        found.setdefault("season", int(m.group(1)))
        found.setdefault("episode", int(m.group(2)))
        take(m.start())

    m = re.search(r"(?<!\d)(\d{1,2})[xX](\d{2,4})(?!\d)", text)
    if m:
        found.setdefault("season", int(m.group(1)))
        found.setdefault("episode", int(m.group(2)))
        take(m.start())

    m = re.search(r"[Ss]eason[\s._-]*(\d{1,2})", text, re.I)
    if m:
        found.setdefault("season", int(m.group(1)))
        take(m.start())

    m = re.search(r"第\s*(\d{1,2})\s*季", text) or _CN_NUMBER_RE.search(text)
    if m:
        number = _cn_number(m.group(1))
        if number:
            found.setdefault("season", number)
            take(m.start())

    m = re.search(r"第\s*(\d{1,4})\s*[集话話期]", text)
    if m:
        number = int(m.group(1))
        if 0 < number <= 2000:
            found.setdefault("episode", number)
            take(m.start())

    # [E02] / EP02 / .E02.
    m = re.search(r"(?:^|[\s._\-\[【])[Ee][Pp]?[\s._-]*(\d{1,4})(?!\d)", text)
    if m:
        number = int(m.group(1))
        if 0 < number <= 2000:
            found.setdefault("episode", number)
            take(m.start())

    # 字幕组常见的「 - 01」写法，排除 480/720/1080 这类分辨率。
    for m in re.finditer(r"(?:^|[\s._])-\s*(\d{1,3})(?=[\s._\-\]】]|$)", text):
        number = int(m.group(1))
        if number in (480, 720, 1080, 2160):
            continue
        if 0 < number <= 999:
            found.setdefault("episode", number)
            take(m.start())
            break

    # 「[01]」「(02)」这种方括号集号。
    for m in re.finditer(r"[\[【(（]\s*(\d{1,3})\s*[\]】)）]", text):
        number = int(m.group(1))
        if number in (480, 720, 1080, 2160):
            continue
        if 0 < number <= 999:
            found.setdefault("episode", number)
            take(m.start())
            break

    # 「庆余年 第二季 01」：已经有季号时，结尾的独立数字按集号处理。
    if "season" in found and "episode" not in found:
        m = re.search(r"(?<!\d)(\d{1,3})\s*$", text)
        if m:
            number = int(m.group(1))
            if number not in (480, 720, 1080, 2160) and 0 < number <= 999:
                found["episode"] = number
                take(m.start())

    # 文件本身就是「01」「第03话」这种纯集号时，也要能把整个名字当标记。
    if not found:
        m = re.fullmatch(r"\s*(\d{1,4})\s*", text)
        if m:
            number = int(m.group(1))
            if 0 < number <= 2000:
                found["episode"] = number
                cut = 0

    return found, cut


def _year_matches(text: str):
    """合理的年份匹配：排除分辨率（`1920x1040`）里的数字。"""
    limit = date.today().year + 2
    for m in _YEAR_RE.finditer(text):
        value = int(m.group(1))
        if not (1900 <= value <= limit):
            continue
        if _RESOLUTION_TAIL_RE.match(text, m.end()):
            continue
        if _RESOLUTION_HEAD_RE.search(text[: m.start()]):
            continue
        yield m


def _extract_year(text: str) -> int | None:
    """取最后一个合理的年份：发布年份通常写在画质标记前面。"""
    years = [int(m.group(1)) for m in _year_matches(text)]
    return years[-1] if years else None


def _title_cut(text: str) -> int:
    """片名结束的位置：年份、画质标记之前的部分才是片名。"""
    cut = len(text)
    for m in _year_matches(text):
        if m.start() > 0:
            cut = min(cut, m.start())
            break
    quality = _QUALITY_RE.search(text)
    if quality and quality.start() > 0:
        cut = min(cut, quality.start())
    return cut


def _head_title(text: str) -> str:
    """从开头的 [..][..] 串里挑片名；挑不出中文片名时返回空串。

    发布名常写成 `[中文名][外文名][音轨字幕]`，带中文的方括号往往就是片名，
    不能整段当字幕组丢掉。取最后一个像片名的中文方括号，避开标签。
    """
    title = ""
    pos = 0
    while True:
        m = _HEAD_BRACKET_RE.match(text, pos)
        if not m:
            break
        name = m.group(1).strip()
        pos = m.end()
        while pos < len(text) and text[pos] in " \t._-":
            pos += 1
        if not name or not _CJK_RE.search(name):
            continue
        if _CN_JUNK_RE.search(name) or _GROUP_TAG_RE.search(name):
            continue
        title = name
    return title


def _remember_alt(alts: list[str], value: str) -> None:
    """记下被切掉的外文原名，供多标题交叉校验；太短的片段不参与。"""
    alt = value.strip(" -_·:：|")
    if len(norm_key(alt)) >= 2 and alt not in alts:
        alts.append(alt)


def _clean_titles(text: str) -> tuple[str, list[str]]:
    """清理片名，并顺带取出被丢弃的外文原名。

    `The Wind Rises 風立ちぬ` 这类名字里同时有外文原名和中/日文名，
    主片名仍取中/日文，外文名留作检索时的第二个候选。
    """
    # 网盘里有些文件名带 HTML 转义（`Howl&#39;s`），先还原成正常字符，
    # 否则英文原名切不干净，主片名会连外文一起留下。
    text = html.unescape(text or "").replace("\u200b", " ")
    head = _head_title(text)
    if head:
        # `[风之谷][Nausicaa]...`：中文方括号就是片名，后面的外文名和标签都丢掉。
        text = head
    else:
        # 开头的 [字幕组] / 【发布组】 整段丢掉。
        text = re.sub(r"^\s*[\[【][^\]】]{1,40}[\]】]\s*", "", text)
    # 去掉括号本身，里面的中字/画质标签随后会被 _CN_JUNK_RE 清掉。
    text = re.sub(r"[\[\]【】{}()（）]", " ", text)
    text = _QUALITY_RE.sub(" ", text)
    text = _RESOLUTION_RE.sub(" ", text)
    text = _VOICE_TAG_RE.sub(" ", text)
    text = _CN_JUNK_RE.sub(" ", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    alts: list[str] = []
    head_match = _FOREIGN_HEAD_RE.match(text)
    if head_match:
        _remember_alt(alts, head_match.group(0))
    text = _FOREIGN_HEAD_RE.sub("", text)
    tail_match = _FOREIGN_TAIL_RE.search(text)
    if tail_match:
        _remember_alt(alts, tail_match.group(0))
    text = _FOREIGN_TAIL_RE.sub("", text)
    text = text.strip(" -_·:：|")
    # 末尾的字幕组标记，如 -RARBG / -CMCT
    text = re.sub(r"\s*-\s*[A-Za-z0-9]{2,12}$", "", text).strip(" -_·")
    return text, alts


def pick_folder(parts: list[str]) -> str:
    """从相对目录里挑一个能当片名用的目录名。

    跳过「Season 01」「第一季」这类季目录和电影/电视剧分类目录，
    让「Show/Season 1/01.mkv」也能拿到 Show 作为剧名。
    """
    for part in reversed(parts):
        name = (part or "").strip()
        if not name or name in LIBRARY_DIRS or name.lower() in _GENERIC_TITLES:
            continue
        if _SEASON_DIR_RE.match(name):
            continue
        return name
    return ""


def parse_media(name: str, folder: str = "") -> dict[str, Any]:
    """把文件名拆成片名、年份、季集。

    folder 是文件所在目录名：文件名只有「01.mkv」时用目录名补片名和季号。
    """
    stem = Path(name or "").stem
    folder = (folder or "").strip()
    marker, cut = _find_markers(stem)
    season = marker.get("season")
    episode = marker.get("episode")

    # 季号也经常写在目录上，如「庆余年 第二季 / 01.mkv」。
    if season is None and folder:
        folder_marker, _ = _find_markers(folder)
        season = folder_marker.get("season")

    cut = min(cut, _title_cut(stem))
    title, alts = _clean_titles(stem[:cut] if cut < len(stem) else stem)
    year = _extract_year(stem)

    if (not title or title.lower() in _GENERIC_TITLES) and folder:
        _, folder_cut = _find_markers(folder)
        folder_cut = min(folder_cut, _title_cut(folder))
        folder_title, folder_alts = _clean_titles(
            folder[:folder_cut] if folder_cut < len(folder) else folder
        )
        if folder_title and folder_title.lower() not in _GENERIC_TITLES:
            title = folder_title
            alts = folder_alts

    if year is None and folder:
        year = _extract_year(folder)

    if not title:
        title = "未命名"

    if episode:
        kind = "episode"
        confidence = "high" if title != "未命名" else "low"
        reason = f"剧集 S{season or 1:02d}E{episode:02d}"
    else:
        kind = "movie"
        if year:
            confidence = "medium"
            reason = f"电影 {year}"
        else:
            confidence = "low"
            reason = "未识别到年份"

    # 和外文原名交叉校验时，重复的、被删空的候选没有意义。
    title_key = norm_key(title)
    alt_titles = [
        alt for alt in alts if norm_key(alt) and norm_key(alt) != title_key
    ]

    return {
        "title": title,
        "year": year,
        "kind": kind,
        "season": season,
        "episode": episode,
        "confidence": confidence,
        "reason": reason,
        "alt_titles": alt_titles,
    }


def target_paths(item: dict[str, Any], ext: str) -> tuple[list[str], str]:
    """按 Jellyfin/Emby 习惯生成目标目录与文件名。"""
    title = safe_component(str(item.get("title") or "未命名"))
    year = item.get("year")
    label = f"{title} ({year})" if year else title
    if item.get("kind") == "episode":
        season = int(item.get("season") or 1)
        episode = int(item.get("episode") or 1)
        return (
            [TV_DIR, label, f"Season {season:02d}"],
            safe_component(f"{title} S{season:02d}E{episode:02d}{ext}"),
        )
    return [MOVIE_DIR, label], safe_component(f"{label}{ext}")


def norm_key(text: str) -> str:
    # 先拆掉音标符号，`Nausicaä` 才能和 `Nausicaa` 对上。
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # 片假名/平假名是日文片名的一部分，必须保留：删掉不仅会让纯假名标题
    # 变成空串，还会让不同假名标题塌缩成同一个 key（`風立ちぬ` → `風立`）。
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", "", text.lower())


def _first_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class MetaMatch:
    title: str
    year: int | None
    source_id: str
    # 命中路径：tmdb=TMDB 检索，cache=本地缓存。
    via: str = ""


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _simplify(text: str) -> str:
    """繁转简，把「侧耳傾聽」这类港台写法换成 TMDB 的简体标题；没变化就返回空串。"""
    if _zh_convert is None or not _CJK_RE.search(text):
        return ""
    converted = _zh_convert(text, "zh-cn").strip()
    return converted if converted and converted != text else ""


def _query_tails(query: str) -> list[str]:
    """「Omohide Poro Poro Only Yesterday」这类罗马音前缀 + 英文原名混写，
    整串在 TMDB 搜不到；从第二个词起逐段截掉开头再试。"""
    words = query.split()
    if len(words) < 3:
        return []
    return [" ".join(words[i:]) for i in range(1, len(words) - 1)]


_ALIAS_YEAR_RE = re.compile(r"[（(]?\s*(19\d{2}|20\d{2})\s*[)）]?\s*$")


def parse_aliases(text: str) -> dict[str, tuple[str, int | None]]:
    """解析设置里的片名别名，每行「本地名 = 目标片名 (年份)」。

    TMDB 没有对应条目、或条目名对不上时用它兜底，例如
    「吉卜力实验剧场 = On Your Mark (1995)」。
    """
    aliases: dict[str, tuple[str, int | None]] = {}
    for line in (text or "").replace("＝", "=").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        local, sep, target = line.partition("=")
        local, target = local.strip(), target.strip()
        if not sep or not local or not target:
            continue
        year = None
        matched = _ALIAS_YEAR_RE.search(target)
        if matched:
            year = int(matched.group(1))
            target = target[: matched.start()].strip()
        if target:
            aliases[norm_key(local)] = (target, year)
    return aliases


# 中文标题在别名里按这个顺序挑：大陆优先，其次港台。
_LANGUAGE_REGIONS = {
    "zh-CN": ("CN", "HK", "TW"),
    "zh-TW": ("TW", "HK", "CN"),
}

# 精确匹配不上时，最多翻前几条候选的别名，免得一次校正打出太多请求。
_ALIAS_LIMIT = 3
# 别名命中的分数低于主标题精确匹配（100）。
_ALIAS_SCORE = 80.0
# 检索结果只剩一条时，TMDB 其实已经回答了这次查询。
_ONLY_SCORE = 60.0

# 国内网络直连 api.themoviedb.org 经常超时。CheckTMDB 会维护一份可用节点 IP
# （hosts 格式），请求直接连节点 IP、按域名做 TLS 校验，绕开被污染的 DNS。
_TMDB_HOST = "api.themoviedb.org"
_TMDB_HOSTS_TTL = 6 * 3600.0
# 没取到列表或节点连不通时的重试间隔。
_TMDB_IP_RETRY = 600.0
# (列表地址, 过期时间, IP)；IP 为空表示上一轮没拿到，到点再试。
_tmdb_ip: tuple[str, float, str] | None = None


async def _tmdb_api_ip(url: str) -> str:
    """按配置的 hosts 列表地址取一个 TMDB 节点 IP；拿不到就返回空串，退回正常 DNS。"""
    global _tmdb_ip
    url = (url or "").strip()
    if not url:
        return ""
    now = time.monotonic()
    if _tmdb_ip and _tmdb_ip[0] == url and now < _tmdb_ip[1]:
        return _tmdb_ip[2]
    ip = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=4.0)) as http:
            resp = await http.get(url)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == _TMDB_HOST:
                ip = parts[0]
                break
    except Exception:
        ip = ""
    _tmdb_ip = (url, now + (_TMDB_HOSTS_TTL if ip else _TMDB_IP_RETRY), ip)
    return ip


def _drop_tmdb_ip(url: str, ip: str) -> None:
    """节点连不通就丢掉，本次请求改走正常 DNS。"""
    global _tmdb_ip
    if _tmdb_ip and _tmdb_ip[0] == url and _tmdb_ip[2] == ip:
        _tmdb_ip = (url, time.monotonic() + _TMDB_IP_RETRY, "")


class TmdbError(RuntimeError):
    """TMDB 返回了不可重试的错误，例如 Key 无效。"""


class TmdbClient:
    """TMDB v3 检索：免费 Key，官方限流约 50 次/秒。

    标题语言由设置里的 `tmdb_language` 决定；选中文时优先中文译名，没有中文名
    再退中文别名、最后保留本地中文片名。只接受与本地解析片名完全一致的条目，
    避免把相近的片子改错。
    """

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(
        self,
        api_key: str,
        hosts_url: str = "",
        language: str = "zh-CN",
        aliases: dict[str, tuple[str, int | None]] | None = None,
        llm: LlmClient | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        # CheckTMDB 的 hosts 列表地址；留空就直连官方域名。
        self.hosts_url = (hosts_url or "").strip()
        # 片名语言：zh-CN / zh-TW / en-US。
        self.language = (language or "zh-CN").strip()
        # 本地片名 → (目标片名, 年份)，TMDB 缺条目时的手动映射。
        self.aliases = aliases or {}
        # 可选的文件名兜底解析，没配置时为 None。
        self.llm = llm
        # 只有选中文时才需要做「别名词条 / 保留本地中文名」的回退。
        self._chinese = self.language.startswith("zh")
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            headers={"Accept": "application/json"},
        )
        # v4 的读访问令牌是 JWT，用 Authorization 头；v3 的 32 位 Key 走查询参数。
        self._bearer = self.api_key.startswith("eyJ")

    async def close(self) -> None:
        await self._http.aclose()

    def _params(self, **extra: Any) -> dict[str, Any]:
        params = dict(extra)
        if not self._bearer:
            params["api_key"] = self.api_key
        return params

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self._bearer else {}

    async def _request(self, path: str, **params: Any) -> httpx.Response:
        headers = self._headers()
        ip = await _tmdb_api_ip(self.hosts_url)
        if ip:
            try:
                return await self._http.get(
                    f"https://{ip}/3/{path}",
                    params=self._params(**params),
                    # 连的是节点 IP，Host 和 TLS SNI 仍用官方域名，证书校验才有效。
                    headers={**headers, "Host": _TMDB_HOST},
                    extensions={"sni_hostname": _TMDB_HOST},
                )
            except httpx.TransportError:
                _drop_tmdb_ip(self.hosts_url, ip)
        return await self._http.get(
            f"{self.BASE_URL}/{path}", params=self._params(**params), headers=headers
        )

    async def _get(self, path: str, **params: Any) -> Any:
        resp = await self._request(path, **params)
        if resp.status_code in (401, 403):
            raise TmdbError("TMDB API Key 无效或无权限")
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _results(data: Any) -> list[Any]:
        results = data.get("results") if isinstance(data, dict) else None
        return results if isinstance(results, list) else []

    async def search(
        self,
        title: str,
        kind: str,
        year: int | None,
        alt_titles: list[str] | None = None,
        raw_name: str = "",
    ) -> MetaMatch | None:
        title = (title or "").strip()
        if not title or title == "未命名":
            return None
        # 设置里配了片名别名时，直接按映射的目标片名检索；查不到也按映射整理。
        alias = self.aliases.get(norm_key(title))
        alias_title = ""
        if alias:
            alias_title = alias[0]
            title = alias_title
            year = alias[1] or year
        alts = []
        if not alias_title:
            alts = [alt.strip() for alt in alt_titles or [] if alt.strip()]
        is_tv = kind == "episode"
        path = "tv" if is_tv else "movie"
        # 不传年份过滤：本地解析的年份经常差一年，交给 _rank 打分更稳。
        query = title
        results = self._results(
            await self._get(
                f"search/{path}", query=query, language=self.language, include_adult="false"
            )
        )
        best = self._rank(results, query, is_tv, year)
        if not results:
            # 繁体写法 TMDB 搜不到，换成简体再试一次。
            simplified = _simplify(query)
            if simplified:
                query = simplified
                results = self._results(
                    await self._get(
                        f"search/{path}",
                        query=query,
                        language=self.language,
                        include_adult="false",
                    )
                )
                best = self._rank(results, query, is_tv, year)
        if alts:
            # 文件名里同时写了外文原名时，两个标题各自精确命中的条目求交集，
            # 只剩一条才算确定，不再靠年份和热度在同名翻拍之间猜。
            agreed = await self._consensus(path, results, query, alts, is_tv, year)
            if agreed is not None:
                best = agreed
        if best is None and self._chinese and not _CJK_RE.search(query):
            # 纯外文片名在中文结果里只剩译名，匹配不上；换英文确认条目，
            # 再按条目 id 取中文译名，避免整理成外文原名。
            alt_results, best = await self._en_lookup(path, query, is_tv, year)
            if best is None:
                results = alt_results
        if best is None and not results and not _CJK_RE.search(query):
            # 罗马音前缀 + 英文原名混写时整串检索不到，逐段截掉开头的词再试。
            for tail in _query_tails(query):
                tail_results, best = await self._en_lookup(path, tail, is_tv, year)
                if best is not None:
                    results = tail_results
                    break
        if best is None:
            # 本地片名和 TMDB 主标题对不上（港台译名、原版改名），翻候选条目的别名再比。
            best = await self._alias_rank(results, query, is_tv, year, path)
        if best is None:
            # `红の豚` 这类繁简混写的片名搜出来只有一条，就是 TMDB 给出的答案。
            best = self._only_result(results, query, is_tv, year)
        if best is None and not alias_title:
            # 本地规则彻底对不上时，让模型给几个候选片名，再回 TMDB 精确确认。
            best = await self._llm_fallback(path, is_tv, year, raw_name or title)
        if best is None:
            if alias_title:
                # 手动映射优先于检索结果：TMDB 没这条也按配置的片名整理。
                return MetaMatch(title=alias_title, year=year, source_id="", via="alias")
            return None
        matched = best[1]
        # 选中文时结果里可能是外文名（英文兜底检索出来的条目），先按条目 id
        # 取回目标语言主标题，如 `Warriors of the Wind` 的中文名是「风之谷」；
        # 再退别名，最后保留本地中文片名，不把用户的片名改成外文原名。
        if self._chinese and not _CJK_RE.search(matched):
            detail = await self._detail(str(best[3]), path)
            if detail:
                matched = detail[0]
                if best[2] is None:
                    best = (best[0], matched, detail[1], best[3], best[4])
            if not _CJK_RE.search(matched):
                zh = await self._alternative(str(best[3]), path)
                if zh:
                    matched = zh
                elif _CJK_RE.search(title):
                    matched = title
        # 手动映射带了年份时，条目缺上映日期就用映射里的年份。
        matched_year = best[2]
        if alias_title and matched_year is None:
            matched_year = year
        return MetaMatch(
            title=matched, year=matched_year, source_id=str(best[3]), via="tmdb"
        )

    async def _llm_fallback(
        self, path: str, is_tv: bool, year: int | None, raw_name: str
    ) -> tuple[float, str, int | None, Any, float] | None:
        """文件名里没有可用片名时，让模型给候选片名，再回 TMDB 精确确认。

        模型只负责出候选，不做裁决：候选必须标题精确命中、年份在一年误差内，
        并且唯一命中才算数；不满足就当作没查到，整理仍用本地片名。
        """
        if self.llm is None:
            return None
        try:
            hint = await self.llm.suggest(
                raw_name, "episode" if is_tv else "movie", year
            )
        except Exception as exc:  # 兜底失败不影响主流程
            log.warning("LLM 兜底解析失败：%s", exc)
            return None
        if not hint:
            return None
        guesses: list[str] = []
        for value in [hint.get("title"), *hint.get("alt_titles", [])]:
            text = str(value or "").strip()
            if text and text not in guesses:
                guesses.append(text)
        for guess in guesses:
            key = norm_key(guess)
            if not key:
                continue
            results = self._results(
                await self._get(
                    f"search/{path}",
                    query=guess,
                    language=self.language,
                    include_adult="false",
                )
            )
            hits = []
            for raw in results[:20]:
                meta = self._candidate(raw, is_tv)
                if not meta or not meta["id"]:
                    continue
                if key not in {norm_key(meta["localized"]), norm_key(meta["original"])}:
                    continue
                # 本地年份可能差一年，媒体条目也常标制作年而非首映年。
                if year and meta["year"] and abs(meta["year"] - year) > 1:
                    continue
                hits.append(meta)
            if len(hits) == 1:
                meta = hits[0]
                score = self._score(
                    100.0, meta, year, norm_key(meta["localized"]) == key
                )
                return (
                    score,
                    meta["label"],
                    meta["year"],
                    meta["id"],
                    meta["popularity"],
                )
        return None

    async def _consensus(
        self,
        path: str,
        results: list[Any],
        title: str,
        alt_titles: list[str],
        is_tv: bool,
        year: int | None,
    ) -> tuple[float, str, int | None, Any, float] | None:
        """多个标题各自精确命中的条目求交集，唯一交集才采用。

        文件名常把中文译名和外文原名写在一起（`The Wind Rises 風立ちぬ`），
        两个标题都指向同一条时，就不用再靠年份或热度在同名翻拍里挑。
        交集为空或不止一条说明证据不足，交回原来的打分逻辑。
        """
        target = norm_key(title)
        if not target:
            return None
        main_ids = {
            str(meta["id"])
            for raw in results[:20]
            if (meta := self._candidate(raw, is_tv))
            and meta["id"]
            and target in {norm_key(meta["localized"]), norm_key(meta["original"])}
        }
        agreed: set[str] | None = None
        for alt in alt_titles:
            key = norm_key(alt)
            if not key or key == target:
                continue
            data = await self._get(
                f"search/{path}", query=alt, language="en-US", include_adult="false"
            )
            ids = {
                str(meta["id"])
                for raw in self._results(data)[:20]
                if (meta := self._candidate(raw, is_tv))
                and meta["id"]
                and key in {norm_key(meta["localized"]), norm_key(meta["original"])}
            }
            if not ids:
                continue
            agreed = ids if agreed is None else agreed & ids
            if not agreed:
                return None
        if not agreed:
            return None
        if main_ids:
            agreed &= main_ids
        if len(agreed) != 1:
            return None
        media_id = next(iter(agreed))
        for raw in results[:20]:
            meta = self._candidate(raw, is_tv)
            if meta and str(meta["id"]) == media_id:
                score = self._score(
                    100.0, meta, year, norm_key(meta["localized"]) == target
                )
                return (
                    score,
                    meta["label"],
                    meta["year"],
                    meta["id"],
                    meta["popularity"],
                )
        detail = await self._detail(media_id, path)
        if detail:
            return (100.0, detail[0], detail[1] or year, media_id, 0.0)
        return None

    async def _en_lookup(
        self, path: str, query: str, is_tv: bool, year: int | None
    ) -> tuple[list[Any], tuple[float, str, int | None, Any, float] | None]:
        """用英文检索确认条目，命中后按 id 取回目标语言的译名。"""
        results = self._results(
            await self._get(
                f"search/{path}", query=query, language="en-US", include_adult="false"
            )
        )
        best = self._rank(results, query, is_tv, year)
        if best is not None:
            detail = await self._detail(str(best[3]), path)
            if detail:
                best = (best[0], detail[0], detail[1] or best[2], best[3], best[4])
        return results, best

    async def _alternative(self, media_id: str, path: str) -> str:
        """从条目的别名里挑一个目标语言的标题；没有就返回空串。"""
        regions = _LANGUAGE_REGIONS.get(self.language)
        if not media_id or not regions:
            return ""
        try:
            data = await self._get(f"{path}/{media_id}/alternative_titles")
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        items = data.get("titles" if path == "movie" else "results")
        best = ""
        best_rank = len(regions) + 1
        for item in items or []:
            if not isinstance(item, dict):
                continue
            alt = str(item.get("title") or "").strip()
            if not alt or not _CJK_RE.search(alt):
                continue
            region = str(item.get("iso_3166_1") or "").upper()
            lang = str(item.get("iso_639_1") or "").lower()
            if region in regions:
                rank = regions.index(region)
            elif lang in ("zh", "cn"):
                rank = len(regions)
            else:
                continue
            if rank < best_rank:
                best, best_rank = alt, rank
        return best

    async def _detail(self, media_id: str, path: str) -> tuple[str, int | None] | None:
        """按条目 id 取目标语言译名；取不到就返回 None，沿用检索结果。"""
        if not media_id:
            return None
        data = await self._get(f"{path}/{media_id}", language=self.language)
        if not isinstance(data, dict):
            return None
        title = str(data.get("name" if path == "tv" else "title") or "").strip()
        date = str(data.get("first_air_date" if path == "tv" else "release_date") or "")
        if not title:
            return None
        return title, (_first_int(date[:4]) if date else None)

    @staticmethod
    def _candidate(item: Any, is_tv: bool) -> dict[str, Any] | None:
        """把一条检索结果整理成打分需要的字段。"""
        if not isinstance(item, dict):
            return None
        localized = str(item.get("name" if is_tv else "title") or "").strip()
        original = str(item.get("original_name" if is_tv else "original_title") or "").strip()
        if not localized and not original:
            return None
        date = str(item.get("first_air_date" if is_tv else "release_date") or "")
        return {
            "label": localized or original,
            "localized": localized,
            "original": original,
            "year": _first_int(date[:4]) if date else None,
            "id": item.get("id") or "",
            "popularity": float(item.get("popularity") or 0.0),
        }

    @staticmethod
    def _score(
        base: float, meta: dict[str, Any], year: int | None, localized_hit: bool
    ) -> float:
        score = base
        item_year = meta["year"]
        if year and item_year:
            score += 40 if item_year == year else (-20 if abs(item_year - year) > 1 else 5)
        # 有中文译名的条目优先，避免整理成外文原名。
        if localized_hit:
            score += 10
        return score

    @staticmethod
    def _better(
        best: tuple[float, str, int | None, Any, float] | None,
        score: float,
        meta: dict[str, Any],
    ) -> tuple[float, str, int | None, Any, float]:
        # 同名时按热度取更主流的条目，免得抓到翻拍或真人版。
        if (
            best is None
            or score > best[0]
            or (score == best[0] and meta["popularity"] > best[4])
        ):
            return (score, meta["label"], meta["year"], meta["id"], meta["popularity"])
        return best

    @classmethod
    def _rank(
        cls, results: list[Any], title: str, is_tv: bool, year: int | None
    ) -> tuple[float, str, int | None, Any, float] | None:
        target = norm_key(title)
        best: tuple[float, str, int | None, Any, float] | None = None
        for raw in results[:20]:
            meta = cls._candidate(raw, is_tv)
            if meta is None:
                continue
            localized = norm_key(meta["localized"]) if meta["localized"] else ""
            original = norm_key(meta["original"]) if meta["original"] else ""
            if target not in {localized, original} or not target:
                continue
            # 条目没有本地化译名时 TMDB 直接用原名兜底，本地化命中和原名命中
            # 就成了一回事；这种情况不加分，否则会压过热度更高的正片。
            localized_hit = localized == target and localized != original
            score = cls._score(100.0, meta, year, localized_hit)
            best = cls._better(best, score, meta)
        return best

    @classmethod
    def _only_result(
        cls, results: list[Any], title: str, is_tv: bool, year: int | None
    ) -> tuple[float, str, int | None, Any, float] | None:
        """检索结果只有一条且片名有辨识度时直接采用，别让繁简/罗马音差异挡住。"""
        if len(results) != 1 or len(norm_key(title)) < 2:
            return None
        meta = cls._candidate(results[0], is_tv)
        if meta is None:
            return None
        score = cls._score(_ONLY_SCORE, meta, year, False)
        return (score, meta["label"], meta["year"], meta["id"], meta["popularity"])

    async def _alias_titles(self, media_id: str, path: str) -> list[str]:
        """条目的全部别名，用于港台译名、原版改名这类主标题对不上的情况。"""
        if not media_id:
            return []
        try:
            data = await self._get(f"{path}/{media_id}/alternative_titles")
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        items = data.get("titles" if path == "movie" else "results")
        names: list[str] = []
        for item in items or []:
            if isinstance(item, dict):
                value = str(item.get("title") or "").strip()
                if value:
                    names.append(value)
        return names

    async def _alias_rank(
        self,
        results: list[Any],
        title: str,
        is_tv: bool,
        year: int | None,
        path: str,
    ) -> tuple[float, str, int | None, Any, float] | None:
        """主标题都没对上时，翻前几条候选的别名再比一次。"""
        target = norm_key(title)
        if not target:
            return None
        best: tuple[float, str, int | None, Any, float] | None = None
        checked = 0
        for raw in results[:20]:
            meta = self._candidate(raw, is_tv)
            if meta is None:
                continue
            if checked >= _ALIAS_LIMIT:
                break
            checked += 1
            aliases = await self._alias_titles(str(meta["id"]), path)
            if not any(norm_key(name) == target for name in aliases):
                continue
            score = self._score(_ALIAS_SCORE, meta, year, False)
            best = self._better(best, score, meta)
        return best
