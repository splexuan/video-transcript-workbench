"""兜底解析接口：主链路失败时，用第三方聚合接口拿视频直链。

默认网关是 BugPk-Api（https://api-new.ifphp.com），需要在配置里填 API Key。
接口返回带时效签名的直链，只能现取现用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from .errors import FallbackError, FallbackKeyMissingError
from .types import MediaInfo

logger = logging.getLogger(__name__)

AGGREGATE_PATH = "/api/svparse"
PLATFORM_PATHS: dict[str, tuple[str, ...]] = {
    "bilibili": ("/api/bilibili", AGGREGATE_PATH),
    "douyin": ("/api/dyjx", AGGREGATE_PATH),
    "kuaishou": ("/api/ksjx", AGGREGATE_PATH),
    "xiaohongshu": (AGGREGATE_PATH,),
    "wechat": ("/api/wxsph", AGGREGATE_PATH),
}
DEFAULT_PATHS = (AGGREGATE_PATH,)
REQUEST_TIMEOUT = 25.0
DOWNLOAD_TIMEOUT = 600.0
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 2000
MAX_PLAUSIBLE_SECONDS = 24 * 3600
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
URL_FIELDS = ("url", "video_url", "play_url", "download_url")
URL_LIST_FIELDS = ("videos", "video_backup", "video_list")
TITLE_FIELDS = ("title", "desc", "description")
DESCRIPTION_FIELDS = ("description", "desc")
UPLOADER_FIELDS = ("author", "auther", "nickname", "name")
UPLOADER_OBJECT_FIELDS = ("user", "author", "auther")
COVER_FIELDS = ("cover", "imgurl", "pic", "image")
NAME_FIELDS = ("name", "nickname", "nickName")


@dataclass(frozen=True, slots=True)
class FallbackMedia:
    title: str
    url: str
    uploader: str | None = None
    thumbnail: str | None = None
    description: str | None = None
    duration_seconds: float | None = None

    def to_media_info(self, platform: str) -> MediaInfo:
        return MediaInfo(
            title=self.title,
            platform=platform,
            duration_seconds=self.duration_seconds,
            uploader=self.uploader,
            thumbnail=self.thumbnail,
            description=self.description,
        )


def _first_text(payload: dict, fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pick_uploader(data: dict) -> str | None:
    direct = _first_text(data, UPLOADER_FIELDS)
    if direct:
        return direct
    for field in UPLOADER_OBJECT_FIELDS:
        nested = data.get(field)
        if isinstance(nested, dict):
            name = _first_text(nested, NAME_FIELDS)
            if name:
                return name
    return None


def _pick_url(data: dict) -> str | None:
    direct = _first_text(data, URL_FIELDS)
    if direct:
        return direct
    for field in URL_LIST_FIELDS:
        items = data.get(field)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    nested = _first_text(item, URL_FIELDS)
                    if nested:
                        return nested
    return None


def _plausible_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < float(value) <= MAX_PLAUSIBLE_SECONDS else None


def _pick_duration(data: dict) -> float | None:
    for field in ("duration", "videoDuration", "duration_seconds"):
        seconds = _plausible_seconds(data.get(field))
        if seconds is not None:
            return seconds
    videos = data.get("videos")
    if isinstance(videos, list) and videos and isinstance(videos[0], dict):
        return _plausible_seconds(videos[0].get("duration"))
    return None


def parse_payload(payload: object) -> FallbackMedia:
    if not isinstance(payload, dict):
        raise FallbackError("兜底解析服务返回的内容无法识别")
    if str(payload.get("code")) != "200":
        message = (
            _first_text(payload, ("error", "msg", "message"))
            or "兜底解析服务没能解析这条链接"
        )
        raise FallbackError(message)

    data = payload.get("data")
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), None)
    if not isinstance(data, dict):
        raise FallbackError("兜底解析服务没有返回作品数据")

    url = _pick_url(data)
    if not url:
        raise FallbackError("兜底解析服务没有拿到视频直链，这条作品可能是图文或需要登录")

    description = _first_text(data, DESCRIPTION_FIELDS)
    return FallbackMedia(
        title=(_first_text(data, TITLE_FIELDS) or "未命名视频")[:MAX_TITLE_CHARS],
        url=url,
        uploader=_pick_uploader(data),
        thumbnail=_first_text(data, COVER_FIELDS),
        description=description[:MAX_DESCRIPTION_CHARS] if description else None,
        duration_seconds=_pick_duration(data),
    )


def _error_message(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return _first_text(payload, ("error", "message", "msg"))


def _resolve_at(path: str, url: str, api_key: str, base_url: str) -> FallbackMedia:
    headers = {"X-API-Key": api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
    try:
        response = httpx.get(
            f"{base_url}{path}",
            params={"url": url},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise FallbackError("兜底解析服务连接失败，请检查网络后重试") from exc

    message = _error_message(response)
    if response.status_code in (401, 403):
        raise FallbackError(
            f"兜底解析服务拒绝了这次请求（{message or 'API Key 无效'}）："
            "请确认 API Key 是否有效、额度是否充足"
        )
    if response.status_code >= 400:
        raise FallbackError(
            message
            or f"兜底解析服务暂时不可用（HTTP {response.status_code}），请稍后重试"
        )
    try:
        return parse_payload(response.json())
    except ValueError as exc:
        raise FallbackError("兜底解析服务返回的内容无法识别") from exc


def resolve(url: str, api_key: str | None, platform: str, base_url: str) -> FallbackMedia:
    if not api_key:
        register_url = f"{base_url.rstrip('/')}/auth/register"
        raise FallbackKeyMissingError(
            "还没有配置兜底解析 API Key。"
            f"打开 {register_url} 用邮箱注册（也可从首页右上角「登录 / 注册」进入），"
            "登录后在「密钥管理」点「新增密钥」，复制完整 Key，"
            "填入技能 config.json 的 fallback_api_key，或设置 VTS_FALLBACK_API_KEY。"
            "只需要发 Key，不要发账号密码。"
        )
    paths = PLATFORM_PATHS.get(platform, DEFAULT_PATHS)
    first_error: FallbackError | None = None
    for path in paths:
        try:
            return _resolve_at(path, url, api_key, base_url)
        except FallbackError as exc:
            if first_error is None:
                first_error = exc
            logger.info("兜底解析 %s 未成功：%s", path, exc)
    assert first_error is not None
    raise first_error


def download(media: FallbackMedia, workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "source.mp4"
    try:
        with httpx.stream(
            "GET",
            media.url,
            headers={"User-Agent": USER_AGENT},
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if content_type.startswith("text/") or "html" in content_type:
                raise FallbackError(
                    f"兜底直链返回的不是视频内容（{content_type or '未知类型'}），请稍后重试"
                )
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(1 << 16):
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        target.unlink(missing_ok=True)
        raise FallbackError("下载兜底解析出的视频失败，请稍后重试") from exc

    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise FallbackError("下载兜底解析出的视频失败，请稍后重试")
    return target
