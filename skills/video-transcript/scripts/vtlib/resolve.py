"""链接解析与音轨下载。

B站 / 抖音 / 视频号走 yt-dlp；快手 / 小红书走分享页；视频号只能走兜底接口。
解析结果里的直链只在本次任务内复用，不跨任务缓存。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .errors import PlatformError, UnsupportedSourceError
from .platforms import platform_label
from .share_pages import resolve_kuaishou, resolve_xiaohongshu
from .types import DirectMedia, MediaInfo

logger = logging.getLogger(__name__)

COOKIE_HINTS = (
    "fresh cookies",
    "cookies are needed",
    "cookies file",
    "cookie is required",
)
LOGIN_HINTS = ("sign in", "log in", "login", "not a bot", "account", "expired")
NO_MEDIA_HINTS = ("no video formats", "no media", "there is no video", "no video found")
UNSUPPORTED_HINTS = ("unsupported url", "unsupported", "unable to extract")
AUDIO_FORMAT_ARGS = ("-f", "bestaudio/best", "-S", "+size")
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 2000
PLACEHOLDER_TITLE = re.compile(r"^[\w ]+ video #\S+$")
TOPIC_MARKER = re.compile(r"\s*#[^#\s]{0,40}\[话题\]#")
DOUYIN_MODAL_ID = re.compile(r"[?&]modal_id=(\d{6,})")
DOUYIN_IMAGE_PATH = re.compile(r"/(?:note|slides|image)/(\d{6,})")
DOUYIN_VIDEO_PATH = re.compile(r"/(?:video|share/video)/(\d{6,})")
DOUYIN_VIDEO_URL = "https://www.douyin.com/video/{0}"
DOWNLOAD_TIMEOUT = 600.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
# 快手/小红书的直链来自移动端分享页，用移动 UA 更稳
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
WECHAT_SHORT_HOSTS = {"weixin.qq.com", "www.weixin.qq.com"}
WECHAT_CHANNELS_HOST = "channels.weixin.qq.com"


def expand_wechat_short_link(url: str) -> str:
    """视频号短链先跟随 301，换成兜底接口能识别的 channels.weixin.qq.com 地址。

    分享出来的 `https://weixin.qq.com/sph/<id>` 直接交给兜底接口时，
    接口经常返回「拿不到视频直链」；展开成 finder-preview 地址后就正常了。
    """

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in WECHAT_SHORT_HOSTS or not parsed.path.startswith("/sph/"):
        return url
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": MOBILE_USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        logger.info("视频号短链展开失败，继续使用原链接：%s", exc)
        return url
    final = str(response.url)
    if WECHAT_CHANNELS_HOST in final:
        logger.info("视频号短链已展开：%s", final)
        return final
    logger.info("视频号短链没有跳到 channels.weixin.qq.com，继续使用原链接")
    return url


def classify_error(message: str, platform: str, has_cookie: bool = False) -> PlatformError:
    label = platform_label(platform)
    lowered = message.lower()
    if any(hint in lowered for hint in COOKIE_HINTS):
        if has_cookie:
            return PlatformError(
                f"{label}拒绝了当前 Cookie：这条作品可能需要登录才能观看，"
                "或当前网络被判定为风险。请更新 Cookie 后重试",
                "COOKIE_INVALID",
            )
        return PlatformError(
            f"{label}需要访问凭据才能解析。请配置 Cookie 后重试，"
            "或配置兜底解析 API Key",
            "COOKIE_REQUIRED",
        )
    if any(hint in lowered for hint in LOGIN_HINTS):
        return PlatformError(
            f"{label}的 Cookie 已失效或权限不足，请重新导出 Cookie 后再试",
            "COOKIE_INVALID",
        )
    if any(hint in lowered for hint in NO_MEDIA_HINTS):
        return PlatformError(
            f"没能从这条{label}内容里取到视频：它可能是图文作品，"
            "也可能是需要登录才能查看的内容",
            "UNSUPPORTED_SOURCE",
        )
    if any(hint in lowered for hint in UNSUPPORTED_HINTS):
        return UnsupportedSourceError(f"暂不支持这个{label}链接，请改用公开的作品链接")
    return PlatformError(f"{label}处理失败：{message}")


def _last_error(result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or "").strip()
    if not stderr:
        return "无法解析该链接"
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "无法解析该链接"


def run_ytdlp(
    args: list[str],
    *,
    cookies_file: Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        *(["--cookies", str(cookies_file)] if cookies_file else []),
        *args,
    ]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlatformError("请求平台超时，请检查网络后重试") from exc


def _follow_redirect(url: str) -> str | None:
    try:
        with httpx.Client(
            follow_redirects=True, timeout=10.0, headers={"User-Agent": USER_AGENT}
        ) as client, client.stream("GET", url) as response:
            final = str(response.url)
    except httpx.HTTPError:
        return None
    return final if final.startswith("http") else None


def normalize_douyin(url: str) -> str:
    """抖音只认 `/video/<id>`；网页版复制出来的多是带 modal_id 的页面地址。"""

    modal = DOUYIN_MODAL_ID.search(url)
    if modal:
        return DOUYIN_VIDEO_URL.format(modal.group(1))
    if DOUYIN_IMAGE_PATH.search(url):
        raise UnsupportedSourceError("这是抖音图文作品，没有音频可以识别")
    video = DOUYIN_VIDEO_PATH.search(url)
    if video:
        return DOUYIN_VIDEO_URL.format(video.group(1))
    resolved = _follow_redirect(url)
    return normalize_douyin(resolved) if resolved else url


def pick_title(payload: dict) -> str:
    title = str(payload.get("title") or "").strip()
    if title and not PLACEHOLDER_TITLE.match(title):
        return title[:MAX_TITLE_CHARS]
    description = str(payload.get("description") or "").strip()
    if description:
        first_line = TOPIC_MARKER.sub("", description.splitlines()[0]).strip()
        if first_line:
            return first_line[:MAX_TITLE_CHARS]
    return title or "未命名视频"


def _normalized_url(url: str, platform: str) -> str:
    return normalize_douyin(url) if platform == "douyin" else url


def resolve_video(
    url: str,
    platform: str,
    cookies_file: Path | None = None,
) -> DirectMedia:
    """返回作品信息以及（快手/小红书才有的）直链列表。"""

    if platform == "kuaishou":
        return resolve_kuaishou(url)
    if platform == "xiaohongshu":
        return resolve_xiaohongshu(url, cookies_file)

    result = run_ytdlp(
        [
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
            _normalized_url(url, platform),
        ],
        cookies_file=cookies_file,
        timeout=120,
    )
    if result.returncode != 0:
        raise classify_error(
            _last_error(result), platform, has_cookie=cookies_file is not None
        )
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise PlatformError("无法解析平台返回的数据") from exc

    info = MediaInfo(
        title=pick_title(payload),
        platform=platform,
        duration_seconds=float(payload["duration"]) if payload.get("duration") else None,
        uploader=str(payload.get("uploader") or "") or None,
        thumbnail=str(payload.get("thumbnail") or "") or None,
        description=str(payload.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS]
        or None,
    )
    return DirectMedia(info=info)


def download_direct(urls: list[str], workspace: Path) -> Path:
    """逐个尝试直链；CDN 出错时可能返回 200 + HTML，这里提前识破。"""

    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "source.mp4"
    last_error: Exception | None = None
    for url in urls:
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"User-Agent": MOBILE_USER_AGENT},
                timeout=DOWNLOAD_TIMEOUT,
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith("text/") or "html" in content_type:
                    raise httpx.HTTPError(
                        f"直链返回的不是视频内容（{content_type or '未知类型'}）"
                    )
                expected = response.headers.get("content-length")
                written = 0
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes(1 << 16):
                        handle.write(chunk)
                        written += len(chunk)
                if expected and expected.isdigit() and written != int(expected):
                    raise httpx.HTTPError(
                        f"下载不完整（{written}/{expected} 字节）"
                    )
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            target.unlink(missing_ok=True)
            continue
        if target.stat().st_size > 0:
            return target
        target.unlink(missing_ok=True)
    raise PlatformError("下载视频失败，请稍后重试") from last_error


def download_audio(
    media: DirectMedia,
    url: str,
    platform: str,
    workspace: Path,
    cookies_file: Path | None = None,
) -> Path:
    """优先用解析阶段拿到的直链；没有直链时交给 yt-dlp 下载音轨。"""

    if media.urls:
        return download_direct(media.urls, workspace)

    workspace.mkdir(parents=True, exist_ok=True)
    output = workspace / "source.%(ext)s"
    result = run_ytdlp(
        [
            *AUDIO_FORMAT_ARGS,
            "--no-playlist",
            "--no-mtime",
            "--output",
            str(output),
            _normalized_url(url, platform),
        ],
        cookies_file=cookies_file,
        timeout=1800,
    )
    candidates = [
        item
        for item in workspace.glob("source.*")
        if item.is_file() and item.suffix not in {".part", ".ytdl"}
    ]
    if result.returncode != 0 or not candidates:
        raise classify_error(
            _last_error(result), platform, has_cookie=cookies_file is not None
        )
    return max(candidates, key=lambda item: item.stat().st_size)
