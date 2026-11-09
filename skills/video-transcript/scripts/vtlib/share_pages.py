"""快手与小红书的分享页解析。

这两个平台没有可用的 yt-dlp extractor，但移动端 UA 的分享页里直出
服务端渲染的数据：快手内嵌多条 mp4 直链，小红书是 `__INITIAL_STATE__`。
这里只做解析，拿到直链后交给 `resolve.download_direct` 下载。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from .cookies import cookie_header
from .errors import PlatformError
from .types import DirectMedia, MediaInfo

KUAISHOU_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
XHS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
REQUEST_TIMEOUT = 25.0
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 2000
MAX_CANDIDATES = 4

# ---- 快手 -----------------------------------------------------------------

KUAI_MEDIA = re.compile(r"""https?://[^\s"'<>\\]+?\.mp4(?:\?[^\s"'<>\\]+)?""")
KUAI_CAPTION = re.compile(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)"')
KUAI_COVER = re.compile(r'"coverUrls"\s*:\s*\[\s*\{[^{}]*?"url"\s*:\s*"([^"]+)"')
KUAI_AUTHOR = re.compile(r'"userName"\s*:\s*"((?:[^"\\]|\\.)*)"')
KUAI_PHOTO_TYPE = re.compile(r'"photoType"\s*:\s*"([^"]*)"')
KUAI_DURATION = re.compile(r'"duration"\s*:\s*(\d+)')
PROMO_HINTS = ("strongMessage", "showCondition")
PROMO_WINDOW = 80

# ---- 小红书 ---------------------------------------------------------------

XHS_STATE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.DOTALL
)
XHS_TOPIC_MARKER = re.compile(r"\s*#[^#\s]{0,40}\[话题\]#")
XHS_CODEC_PRIORITY = ("h264", "h265", "h266", "av1")


def _unescape(text: str) -> str:
    return (
        text.replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\n", " ")
        .strip()
    )


def _fetch(url: str, user_agent: str, cookie: str | None = None) -> tuple[str, str]:
    headers = {"User-Agent": user_agent, "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text, str(response.url)
    except httpx.HTTPError as exc:
        raise PlatformError(f"读取分享页失败，请检查网络后重试：{exc}") from exc


# ---------------------------------------------------------------------------
# 快手
# ---------------------------------------------------------------------------


def _quality_rank(url: str) -> int:
    name = url.split("?")[0].rsplit("/", 1)[-1].lower()
    if "ultra" in name:
        return 3
    if "high" in name:
        return 2
    if "_b_" in name:
        return 0
    return 1


def _rank(links: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for link in links:
        base = link.split("?")[0]
        if base in seen:
            continue
        seen.add(base)
        unique.append(link)
    unique.sort(key=_quality_rank, reverse=True)
    return unique[:MAX_CANDIDATES]


def _kuai_duration(html: str) -> float | None:
    values: list[int] = []
    for match in KUAI_DURATION.finditer(html):
        before = html[max(0, match.start() - PROMO_WINDOW) : match.start()]
        if any(hint in before for hint in PROMO_HINTS):
            continue
        values.append(int(match.group(1)))
    return min(values) / 1000 if values else None


def _parse_kuaishou(html: str, url: str) -> DirectMedia:
    photo_type = KUAI_PHOTO_TYPE.search(html)
    if photo_type is not None and photo_type.group(1).upper() != "VIDEO":
        raise PlatformError("这是快手图集作品，没有音频可以识别", "UNSUPPORTED_SOURCE")

    candidates = _rank(KUAI_MEDIA.findall(html))
    if not candidates:
        raise PlatformError(
            "没能从这条快手内容里取到视频：链接可能已失效、作品已删除，或它不是公开作品",
            "UNSUPPORTED_SOURCE",
        )

    caption = KUAI_CAPTION.search(html)
    text = _unescape(caption.group(1)) if caption else ""
    author = KUAI_AUTHOR.search(html)
    cover = KUAI_COVER.search(html)
    info = MediaInfo(
        title=(text or "未命名视频")[:MAX_TITLE_CHARS],
        platform="kuaishou",
        duration_seconds=_kuai_duration(html),
        uploader=_unescape(author.group(1)) if author else None,
        thumbnail=_unescape(cover.group(1)) if cover else None,
        description=text[:MAX_DESCRIPTION_CHARS] or None,
    )
    return DirectMedia(info=info, urls=candidates)


def resolve_kuaishou(url: str) -> DirectMedia:
    html, _final = _fetch(url, KUAISHOU_UA)
    return _parse_kuaishou(html, url)


# ---------------------------------------------------------------------------
# 小红书
# ---------------------------------------------------------------------------


def _xhs_title(note: dict) -> str:
    title = str(note.get("title") or "").strip()
    if title:
        return title[:MAX_TITLE_CHARS]
    description = str(note.get("desc") or "").strip()
    if description:
        cleaned = XHS_TOPIC_MARKER.sub("", description)
        first_line = next(
            (line.strip() for line in cleaned.splitlines() if line.strip()), ""
        )
        if first_line:
            return first_line[:MAX_TITLE_CHARS]
        topics = re.findall(r"#([^#\s]{1,40})\[话题\]#", description)
        if topics:
            return topics[0][:MAX_TITLE_CHARS]
    return "小红书笔记"


def _xhs_candidates(note: dict) -> list[str]:
    stream = ((note.get("video") or {}).get("media") or {}).get("stream") or {}
    candidates: list[str] = []
    for codec in XHS_CODEC_PRIORITY:
        variants = [item for item in (stream.get(codec) or []) if isinstance(item, dict)]
        if not variants:
            continue
        best = max(variants, key=lambda item: int(item.get("size") or 0))
        url = str(best.get("masterUrl") or best.get("videoUrl") or "")
        if url.startswith("http"):
            candidates.append(url)
    return candidates[:MAX_CANDIDATES]


def _load_state(html: str) -> dict:
    match = XHS_STATE.search(html)
    if match is None:
        raise PlatformError("没能从小红书页面里读到笔记内容，请稍后重试")
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(re.sub(r"\bundefined\b", "null", raw))
    except json.JSONDecodeError as exc:
        raise PlatformError("小红书页面结构发生变化，暂时无法解析，请稍后重试") from exc


def _parse_xhs(state: dict) -> DirectMedia:
    note = ((state.get("noteData") or {}).get("data") or {}).get("noteData") or {}
    if not note:
        raise PlatformError(
            "没能从小红书页面里读到笔记内容：链接可能已失效，或作品被风控拦截"
        )
    if note.get("type") != "video":
        raise PlatformError("这是小红书图文笔记，没有音频可以识别", "UNSUPPORTED_SOURCE")

    candidates = _xhs_candidates(note)
    if not candidates:
        raise PlatformError(
            "没能从这条小红书内容里取到视频：它可能已被删除或仅登录可见",
        )

    video = note.get("video") or {}
    media = video.get("media") or {}
    variants = [
        item
        for group in (media.get("stream") or {}).values()
        for item in (group or [])
        if isinstance(item, dict)
    ]
    best_variant = (
        max(variants, key=lambda item: int(item.get("size") or 0))
        if variants
        else None
    )
    duration_ms = (
        int(best_variant["duration"])
        if best_variant and best_variant.get("duration")
        else None
    )
    frames = note.get("imageList") or []
    cover = (
        str(frames[0].get("urlDefault") or frames[0].get("url") or "") or None
        if frames
        else None
    )
    user = note.get("user") or {}
    uploader = (
        str(user.get("nickName") or user.get("nickname") or "").strip() or None
    )
    description = str(note.get("desc") or "").strip()[:MAX_DESCRIPTION_CHARS] or None
    info = MediaInfo(
        title=_xhs_title(note),
        platform="xiaohongshu",
        duration_seconds=duration_ms / 1000 if duration_ms else None,
        uploader=uploader,
        thumbnail=cover,
        description=description,
    )
    return DirectMedia(info=info, urls=candidates)


def resolve_xiaohongshu(url: str, cookies_file: Path | None = None) -> DirectMedia:
    html, final_url = _fetch(
        url, XHS_UA, cookie=cookie_header(cookies_file, "xiaohongshu")
    )
    if "/404/sec_" in final_url:
        raise PlatformError(
            "这条笔记触发了小红书的风控拦截。可在浏览器里确认能打开，并配置 Cookie "
            "后重试（登录态能显著提高成功率）",
            "COOKIE_REQUIRED",
        )
    if "/login" in final_url:
        raise PlatformError(
            "这条笔记需要登录才能查看：请配置小红书 Cookie 后重试", "COOKIE_REQUIRED"
        )
    return _parse_xhs(_load_state(html))
