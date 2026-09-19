"""平台媒体获取：用 yt-dlp 解析链接、读取字幕、下载音轨。

B站、抖音、小红书共用这一层；平台专属能力（例如 B站的免登录字幕接口）留在各自模块。
需要登录态的平台由调用方传入 Cookie 临时文件，本模块不读取、不保存凭据。
解析结果在同一个任务内会被下载环节复用（见 `_remember_resolution`）。
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TypeVar

import httpx

from app.domain import MediaInfo, TranscriptChunk
from app.infrastructure import kuaishou, xiaohongshu
from app.infrastructure.media import find_tool
from app.infrastructure.subtitles import parse_subtitle

logger = logging.getLogger(__name__)

PLATFORM_LABELS: dict[str, str] = {
    "bilibili": "B站",
    "douyin": "抖音",
    "kuaishou": "快手",
    "xiaohongshu": "小红书",
    "wechat": "视频号",
}

# 平台要求登录态时的下载器提示。抖音这一条最典型：
# "Fresh cookies (not necessarily logged in) are needed"。
COOKIE_HINTS = ("fresh cookies", "cookies are needed", "cookies file", "cookie is required")
LOGIN_HINTS = ("sign in", "log in", "login", "not a bot", "account", "expired")
# 图文类作品（尤其是小红书笔记）没有视频，会走到这里。
NO_MEDIA_HINTS = ("no video formats", "no media", "there is no video", "no video found")
# 音轨下载参数：先要纯音频流，没有才退到音视频合流；再按体积从小到大取。
# 识别只需要 16 kHz 单声道，拉整段高清视频纯属白下载。实测：
# 抖音那条 16 分钟视频 51.5 MB → 27.9 MB，B站 10.0 MB → 4.2 MB（都仍是音轨流）。
# 两个坑：① 不能按分辨率过滤——抖音低分辨率的 download_addr 反而是原始高码率文件（133 MB）；
# ② 不能只写 `-S +size`——那样 B站 会从纯音频退化成音视频合流。
AUDIO_FORMAT_ARGS = ("-f", "bestaudio/best", "-S", "+size")
UNSUPPORTED_HINTS = ("unsupported url", "unsupported", "unable to extract")


class PlatformError(RuntimeError):
    """平台链接解析或媒体下载失败。"""

    code = "PLATFORM_ERROR"


class CookieRequiredError(PlatformError):
    code = "COOKIE_REQUIRED"


class CookieInvalidError(PlatformError):
    code = "COOKIE_INVALID"


class UnsupportedUrlError(PlatformError):
    code = "UNSUPPORTED_SOURCE"


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, "该平台")


def classify_error(message: str, platform: str, has_cookie: bool = False) -> PlatformError:
    """把下载器的原始输出归类成可以直接给用户看的提示。

    `has_cookie` 区分「没配凭据」和「配了但平台不认」：两种情况下载器报的都是
    同一句「需要新鲜 cookies」，但用户该做的事完全不同——后者再去点「一键获取」
    只是白跑一趟，得改用扫码登录。
    """

    label = platform_label(platform)
    lowered = message.lower()
    if any(hint in lowered for hint in COOKIE_HINTS):
        if has_cookie:
            return CookieInvalidError(
                f"{label}拒绝了当前访问凭据：这条作品可能需要登录才能观看，"
                f"或当前网络环境被平台判定为风险。请在「平台连接」页用"
                f"「浏览器登录」扫码后再试"
            )
        return CookieRequiredError(
            f"{label}需要访问凭据才能解析，请在「平台连接」页点击「一键获取访问权限」后重试"
        )
    if any(hint in lowered for hint in LOGIN_HINTS):
        return CookieInvalidError(
            f"{label}的访问凭据已失效或权限不足，请在「平台连接」页重新获取；"
            "需要登录才能观看的内容请改用浏览器登录"
        )
    if any(hint in lowered for hint in NO_MEDIA_HINTS):
        return UnsupportedUrlError(
            f"没能从这条{label}内容里取到视频：它可能是图文笔记，"
            "也可能是需要登录才能查看的内容。请先确认它是视频笔记；"
            "若确实需要登录，请在「平台连接」页用「浏览器登录」后再试一次"
        )
    if any(hint in lowered for hint in UNSUPPORTED_HINTS):
        return UnsupportedUrlError(f"暂不支持这个{label}链接，请改用公开的作品链接或导入本地文件")
    return PlatformError(f"{label}处理失败：{message}")


DOUYIN_VIDEO_URL = "https://www.douyin.com/video/{0}"
# 抖音网页版在精选、发现、搜索、作者主页里打开作品时，地址栏是 `...?modal_id=<作品 id>`。
DOUYIN_MODAL_ID = re.compile(r"[?&]modal_id=(\d{6,})")
DOUYIN_IMAGE_PATH = re.compile(r"/(?:note|slides|image)/(\d{6,})")
DOUYIN_VIDEO_PATH = re.compile(r"/(?:video|share/video)/(\d{6,})")
# 小红书分享文案里的短链：网页版是 xhslink.com，手机端是 xhslink.cn。
# 都要先跟随跳转，最终地址才会带上 xsec_token。
XHS_SHORT_LINK = re.compile(r"^https?://xhslink\.(?:com|cn)/\S+")
XHS_NOTE_PATH = re.compile(r"^https?://[\w.]*xiaohongshu\.com/(?:explore|discovery/item)/[\da-f]+")


def _follow_redirect(url: str, headers: dict[str, str] | None = None) -> str | None:
    """跟随短链跳转拿到最终地址；只读响应头，不下载页面正文。"""

    try:
        with httpx.Client(follow_redirects=True, timeout=10.0, headers=headers) as client, client.stream(
            "GET", url
        ) as response:
            final = str(response.url)
    except httpx.HTTPError as exc:
        logger.info("短链跳转失败：%s", exc)
        return None
    return final if final.startswith("http") else None


def _normalize_douyin(url: str) -> str:
    modal = DOUYIN_MODAL_ID.search(url)
    if modal:
        return DOUYIN_VIDEO_URL.format(modal.group(1))

    if DOUYIN_IMAGE_PATH.search(url):
        raise UnsupportedUrlError("这是图文作品，没有音频可以识别，请改用视频作品或导入本地文件")

    video = DOUYIN_VIDEO_PATH.search(url)
    if video:
        return DOUYIN_VIDEO_URL.format(video.group(1))

    # 分享短链（v.douyin.com/xxx）本身没有作品 id：先本地跳转拿到作品地址，
    # 跳不动就原样交给下载器自己处理
    resolved = _follow_redirect(url)
    if resolved:
        return _normalize_douyin(resolved)
    return url


def _normalize_xiaohongshu(url: str, cookies_file: Path | None = None) -> str:
    if not XHS_SHORT_LINK.match(url):
        return url
    # 移动 UA 即可跟随跳转（桌面 UA 才会被 302 到登录页）；配了凭据一并带上
    resolved = _follow_redirect(url, xiaohongshu.request_headers(cookies_file))
    if resolved and "/login" in resolved:
        raise CookieInvalidError(
            "跟随小红书分享短链时被要求登录：当前访问凭据已失效，"
            "请在「平台连接」页重新获取 Cookie 后重试"
        )
    return resolved or url


def normalize_url(url: str, platform: str, cookies_file: Path | None = None) -> str:
    """把平台的各种链接形式归一成下载器认得的作品地址。

    抖音只认 `/video/<id>`，网页版复制出来的却多是带 `modal_id` 的页面地址；
    小红书分享出来的是 `xhslink.com`/`xhslink.cn` 短链，需要先跳转到带 token 的笔记地址。
    """

    if platform == "douyin":
        return _normalize_douyin(url)
    if platform == "xiaohongshu":
        return _normalize_xiaohongshu(url, cookies_file)
    return url


# 下载器拿不到真实标题时会生成这种占位值，例如 `XiaoHongShu video #6a8c6316...`。
PLACEHOLDER_TITLE = re.compile(r"^[\w ]+ video #\S+$")
TOPIC_MARKER = re.compile(r"\s*#[^#\s]{0,40}\[话题\]#")
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 2000


def pick_title(payload: dict) -> str:
    """挑一个用户能认出来的标题。

    小红书笔记经常只拿到占位标题，此时退回 `description`——它就是这条作品的
    文字内容，比 `XiaoHongShu video #xxx` 有意义得多。描述里的 `#话题[话题]#` 会去掉。
    """

    title = str(payload.get("title") or "").strip()
    if title and not PLACEHOLDER_TITLE.match(title):
        return title

    description = str(payload.get("description") or "").strip()
    if description:
        first_line = TOPIC_MARKER.sub("", description.splitlines()[0]).strip()
        if first_line:
            return first_line[:MAX_TITLE_CHARS]
    return title or "未命名视频"


def _last_error(result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or "").strip()
    if not stderr:
        return "无法解析该链接"
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "无法解析该链接"


def run_ytdlp(
    args: list[str],
    timeout: int = 300,
    cookies_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """调用 yt-dlp；两种运行方式返回同样的 `CompletedProcess`，调用方无需区分。

    - 开发版起子进程：解析器崩溃不会带走主进程，超时也能真的杀掉。
    - 打包版没有独立的 python 解释器（`sys.executable` 就是应用自己，
      `应用.exe -m yt_dlp ...` 只会得到一句 unrecognized arguments），
      因此在同一进程里调用 yt-dlp 的命令行入口。
    """

    common = _ytdlp_common_args(cookies_file)
    if getattr(sys, "frozen", False):
        return _run_ytdlp_in_process([*common, *args])

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-m", "yt_dlp", *common, *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlatformError("请求平台超时，请检查网络后重试") from exc


def _ytdlp_common_args(cookies_file: Path | None) -> list[str]:
    """所有 yt-dlp 调用共用的参数：Cookie 与自带 FFmpeg 的位置。

    打包版把 FFmpeg 放在资源目录里，PATH 上并不存在，而 yt-dlp 合并音视频或转换
    格式时要自己找它——不显式指路就会报「ffmpeg is not installed」。
    """

    common = ["--cookies", str(cookies_file)] if cookies_file is not None else []
    if getattr(sys, "frozen", False):
        ffmpeg = find_tool("ffmpeg")
        if ffmpeg:
            common += ["--ffmpeg-location", ffmpeg]
    return common


def _ytdlp_entry() -> Callable[[list[str] | None], object]:
    """yt-dlp 的命令行入口。延迟导入：子进程路径不需要在主进程里加载它。"""

    from yt_dlp import main

    return main


def _run_ytdlp_in_process(args: list[str]) -> subprocess.CompletedProcess[str]:
    """在打包版里就地跑 yt-dlp 的命令行入口。

    - argv 不能带程序名：yt-dlp 会把多出来的第一个参数当作品地址去下载。
    - `main()` 用 SystemExit 表达退出码，这里换算成 returncode。
    - 同进程调用没有硬超时，超时保护交给 yt-dlp 自己的 socket 超时与重试。
    """

    stdout, stderr = io.StringIO(), io.StringIO()
    code = 0
    try:
        # yt-dlp 在 init 时就抓走 sys.stdout/stderr，所以重定向要包住整个调用
        with redirect_stdout(stdout), redirect_stderr(stderr):
            _ytdlp_entry()(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001
        # yt-dlp 的异常类型很多，统一成一次失败的调用，让调用方按「非零退出码 + stderr」处理
        stderr.write(f"ERROR: {exc}\n")
        code = 1
    return subprocess.CompletedProcess(args, code, stdout.getvalue(), stderr.getvalue())


_MediaT = TypeVar("_MediaT")

# 同一次任务里「解析 → 下载」两步都要拿一次平台直链。直链带时效签名，不能跨任务
# 复用，但两步相隔通常只有几秒：这里保留一个很短的复用窗口，既省掉一次分享页请求，
# 也避免第二次请求被风控拦掉而让整个任务失败（第一次明明已经解析成功）。
RESOLVE_REUSE_SECONDS = 120.0
MAX_RESOLVE_CACHE = 8
_resolve_cache: dict[tuple[str, str], tuple[float, object]] = {}
_resolve_lock = threading.Lock()


def _remember_resolution(platform: str, url: str, media: object) -> None:
    key = (platform, url)
    with _resolve_lock:
        _resolve_cache[key] = (time.monotonic(), media)
        # 桌面端进程长期运行，只留最近几条，避免无上限堆积
        while len(_resolve_cache) > MAX_RESOLVE_CACHE:
            _resolve_cache.pop(next(iter(_resolve_cache)), None)


def _cached_resolution(platform: str, url: str, expected: type[_MediaT]) -> _MediaT | None:
    """取刚解析出的平台结果；已过期或类型不符时按没有处理。"""

    key = (platform, url)
    with _resolve_lock:
        entry = _resolve_cache.get(key)
        if entry is not None and time.monotonic() - entry[0] > RESOLVE_REUSE_SECONDS:
            _resolve_cache.pop(key, None)
            entry = None
    if entry is None:
        return None
    value = entry[1]
    return value if isinstance(value, expected) else None


def _resolve_kuaishou(url: str) -> MediaInfo:
    """快手不走下载器：它既然没有 extractor，就直接读平台自己的分享页。"""

    try:
        media = kuaishou.resolve_media(url)
    except kuaishou.KuaishouError as exc:
        raise PlatformError(str(exc)) from exc
    _remember_resolution("kuaishou", url, media)
    return media.to_media_info()


def _resolve_xiaohongshu(url: str, cookies_file: Path | None = None) -> MediaInfo:
    """小红书解析：HTTP 读分享页（配了凭据就带登录态）；被风控时回退本机浏览器抓取。"""

    try:
        media = xiaohongshu.resolve_with_fallback(url, cookies_file)
    except xiaohongshu.XiaohongshuError as exc:
        raise PlatformError(str(exc)) from exc
    _remember_resolution("xiaohongshu", url, media)
    return media.to_media_info()


def resolve_video(
    url: str,
    platform: str,
    cookies_file: Path | None = None,
) -> MediaInfo:
    if platform == "kuaishou":
        return _resolve_kuaishou(url)

    if platform == "xiaohongshu":
        # 自己解析优先（零依赖、快，配了 Cookie 就带登录态）；失败且用户配了 Cookie
        # 时退回下载器，让下载器那套重试逻辑再兜一次
        try:
            return _resolve_xiaohongshu(url, cookies_file)
        except PlatformError:
            if cookies_file is None:
                raise

    result = run_ytdlp(
        [
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
            normalize_url(url, platform, cookies_file),
        ],
        timeout=120,
        cookies_file=cookies_file,
    )
    if result.returncode != 0:
        raise classify_error(_last_error(result), platform, has_cookie=cookies_file is not None)

    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise classify_error("无法解析平台返回的数据", platform) from exc

    return MediaInfo(
        title=pick_title(payload),
        platform=platform,
        duration_seconds=float(payload["duration"]) if payload.get("duration") else None,
        uploader=str(payload.get("uploader") or "") or None,
        thumbnail=str(payload.get("thumbnail") or "") or None,
        # 作品介绍可能很长，落库前截断，避免把整段简介塞进数据库
        description=str(payload.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS] or None,
    )


def fetch_subtitles(
    url: str,
    workspace: Path,
    platform: str,
    cookies_file: Path | None = None,
) -> list[TranscriptChunk]:
    """尝试读取视频自带字幕；没有字幕时返回空列表。"""

    if platform in {"kuaishou", "xiaohongshu"}:
        # 这两个平台没有可读取的平台字幕，直接走音轨识别，省一次必然失败的请求
        return []

    workspace.mkdir(parents=True, exist_ok=True)
    output = workspace / "subtitle"
    result = run_ytdlp(
        [
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs",
            "zh-Hans,zh-CN,zh,ai-zh,zh-Hant,zh-TW",
            "--sub-format",
            "vtt/srt/best",
            "--skip-download",
            "--no-playlist",
            "--output",
            str(output),
            url,
        ],
        timeout=180,
        cookies_file=cookies_file,
    )
    subtitle_files = sorted(
        [*workspace.glob("subtitle*.vtt"), *workspace.glob("subtitle*.srt")],
        key=lambda item: item.stat().st_size,
        reverse=True,
    )
    if not subtitle_files:
        if result.returncode not in {0, 1}:
            logger.info("读取字幕失败，按没有字幕处理：%s", _last_error(result))
        return []
    return parse_subtitle(subtitle_files[0])


def download_audio_source(
    url: str,
    workspace: Path,
    platform: str,
    cookies_file: Path | None = None,
) -> Path:
    """下载音轨（识别只需要 16 kHz 单声道，不必拉最高画质）。"""
    if platform == "kuaishou":
        try:
            # 复用解析阶段刚拿到的直链，不再请求一次分享页
            return kuaishou.download_media(
                url, workspace, _cached_resolution("kuaishou", url, kuaishou.KuaishouMedia)
            )
        except kuaishou.KuaishouError as exc:
            raise PlatformError(str(exc)) from exc

    if platform == "xiaohongshu":
        # 与 resolve 同样的策略：自建直链优先，失败且配了 Cookie 时退回下载器
        try:
            return xiaohongshu.download_media(
                url,
                workspace,
                _cached_resolution("xiaohongshu", url, xiaohongshu.XiaohongshuMedia),
            )
        except xiaohongshu.XiaohongshuError as exc:
            if cookies_file is None:
                raise PlatformError(str(exc)) from exc

    workspace.mkdir(parents=True, exist_ok=True)
    output = workspace / "source.%(ext)s"
    result = run_ytdlp(
        [
            *AUDIO_FORMAT_ARGS,
            "--no-playlist",
            "--no-mtime",
            "--output",
            str(output),
            normalize_url(url, platform, cookies_file),
        ],
        timeout=1800,
        cookies_file=cookies_file,
    )
    candidates = [
        item
        for item in workspace.glob("source.*")
        if item.is_file() and item.suffix not in {".part", ".ytdl"}
    ]
    if result.returncode != 0 or not candidates:
        raise classify_error(_last_error(result), platform, has_cookie=cookies_file is not None)
    return max(candidates, key=lambda item: item.stat().st_size)
