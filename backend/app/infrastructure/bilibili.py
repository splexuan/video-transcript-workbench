"""B站专属能力：读取未登录也能拿到的平台字幕。

链接解析、字幕回退与音轨下载都在 `infrastructure/platform_media.py`，
这里只保留 B站独有的弹幕元数据接口，避免把平台细节混进通用适配层。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

from app.domain import TranscriptChunk
from app.infrastructure.credential_store import COOKIE_DOMAINS, read_netscape_cookies

logger = logging.getLogger(__name__)

API_BASE = "https://api.bilibili.com"
BV_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
# 字幕语言优先级：B站 AI 中文字幕在前，其次人工上传的中文，最后英文。
SUBTITLE_LANGS = ("ai-zh", "zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "ai-en", "en")
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com",
}


def parse_platform_subtitles(payload: dict) -> list[TranscriptChunk]:
    """把 B站字幕 JSON（body 里的 from/to 以秒为单位）转成统一分段。"""

    chunks: list[TranscriptChunk] = []
    for item in payload.get("body") or []:
        content = str(item.get("content") or "").replace("\n", " ").strip()
        if not content:
            continue
        chunks.append(
            TranscriptChunk(
                start_ms=int(float(item.get("from") or 0) * 1000),
                end_ms=int(float(item.get("to") or 0) * 1000),
                text=content,
            )
        )
    return chunks


def pick_subtitle(subtitles: list[dict]) -> dict | None:
    """按语言优先级挑一条字幕：AI 中文 → 人工中文 → 英文 → 任意可用。"""

    for lang in SUBTITLE_LANGS:
        for item in subtitles:
            if str(item.get("lan")) == lang and item.get("subtitle_url"):
                return item
    for item in subtitles:
        if item.get("subtitle_url"):
            return item
    return None


def _request_headers(cookies_file: Path | None) -> dict[str, str]:
    """组装请求头；用户配了访问凭据就带上，用来读会员或登录可见视频的字幕。"""

    headers = dict(REQUEST_HEADERS)
    cookies = read_netscape_cookies(cookies_file, COOKIE_DOMAINS["bilibili"])
    if cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
    return headers


def fetch_platform_subtitles(
    url: str,
    cookies_file: Path | None = None,
) -> list[TranscriptChunk]:
    """读取平台字幕（含未登录也能拿到的 B站 AI 字幕）。

    走弹幕元数据接口 `x/v2/dm/view`：它对未登录用户同样返回字幕列表，
    比 yt-dlp 用的播放器接口覆盖面更广（后者对 AI 字幕会要求登录）；
    带上 Cookie 还能读到会员与登录可见视频的字幕。
    这里拿不到就返回空列表，由调用方回退到 yt-dlp 或本地识别。
    """

    match = BV_PATTERN.search(url)
    if match is None:
        return []

    try:
        with httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers=_request_headers(cookies_file),
        ) as client:
            view = client.get(
                f"{API_BASE}/x/web-interface/view",
                params={"bvid": match.group(1)},
            )
            view.raise_for_status()
            data = view.json().get("data") or {}
            aid, cid = data.get("aid"), data.get("cid")
            if not aid or not cid:
                return []

            detail = client.get(
                f"{API_BASE}/x/v2/dm/view",
                params={"aid": aid, "oid": cid, "type": 1},
            )
            detail.raise_for_status()
            subtitles = (
                ((detail.json().get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            )
            chosen = pick_subtitle(subtitles)
            if chosen is None:
                return []

            payload = client.get(str(chosen["subtitle_url"]))
            payload.raise_for_status()
            return parse_platform_subtitles(payload.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("平台字幕读取失败：%s", exc)
        return []
