"""兜底解析接口（BugPk-Api）：主链路都失败时，换第三方接口拿无水印直链。

自建解析（快手、小红书分享页）与 yt-dlp 都可能因为平台风控、登录态失效或
页面结构变化而失败。这里接的是 BugPk-Api 的公开网关（已内置，不需要配环境变量）：

    GET {base}/api/svparse?url=<作品链接>      # 短视频解析聚合（API Code: svparse）
    X-API-Key: <在「设置」页配置的 Key>          # 也支持 ?key= 查询参数

API Key 由使用者在 https://api-new.ifphp.com/ 注册账号后自行获取。

按平台分路：文档称聚合端点覆盖 33+ 平台，但实测 B站 走 `/api/svparse` 返回
502「请求失败」，走 `/api/bilibili` 才正常，因此各平台优先用它的专属端点，
失败再退回聚合端点。

设计要点：
- 视频号没有本机解析方案，只能走这里；其它平台仅在主链路失败后使用。
- 没配置 Key 时本模块不参与，其它平台行为与接入前完全一致。
- Key 由调用方传入（存在设置里、本机加密保存），本模块不读配置、不写日志明文。
- 接口返回的是**带时效签名的直链**，只能现取现用，不能跨任务缓存。
- 字段名与文档并不完全一致（B站的作者字段是 `auther`、快手把作者放在
  `author.name`、备选直链在 `video_backup[]`），取值时按别名兜底。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.domain import MediaInfo

logger = logging.getLogger(__name__)

# 内置的兜底解析网关（BugPk-Api）：源码运行与打包版都直接用这个地址，
# 使用者只需要去 https://api-new.ifphp.com/ 注册账号、把 Key 填进「设置」页。
BUILTIN_BASE_URL = "https://api-new.ifphp.com"
# 换域名不用改代码：环境变量覆盖即可。变量缺失、为空或只有空白时仍回落内置网关，
# 免得一个环境变量就让「只能走兜底」的视频号整条链路报「没有配置兜底解析网关」。
BASE_URL = (os.getenv("VTW_FALLBACK_API_BASE") or "").strip().rstrip("/") or BUILTIN_BASE_URL
# 文档里的公开网关：短视频解析聚合
AGGREGATE_PATH = "/api/svparse"
# 各平台的专属端点（实测更可靠）；按顺序尝试，最后退回聚合端点
PLATFORM_PATHS: dict[str, tuple[str, ...]] = {
    "bilibili": ("/api/bilibili", AGGREGATE_PATH),
    "douyin": ("/api/dyjx", AGGREGATE_PATH),
    "kuaishou": ("/api/ksjx", AGGREGATE_PATH),
    "xiaohongshu": (AGGREGATE_PATH,),
    # 视频号没有本机解析方案，只能走这里
    "wechat": ("/api/wxsph", AGGREGATE_PATH),
}
DEFAULT_PATHS = (AGGREGATE_PATH,)

REQUEST_TIMEOUT = 25.0
DOWNLOAD_TIMEOUT = 600.0
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 2000
# 时长的兜底取值范围（秒）：超出这个范围基本是把毫秒当秒了，宁可不要
MAX_PLAUSIBLE_SECONDS = 24 * 3600
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

URL_FIELDS = ("url", "video_url", "play_url", "download_url")
# 备选直链列表：快手把多码率地址放在 video_backup[]，B站合集的每个分集在 videos[]
URL_LIST_FIELDS = ("videos", "video_backup", "video_list")
TITLE_FIELDS = ("title", "desc", "description")
DESCRIPTION_FIELDS = ("description", "desc")
# `auther` 是接口自己的拼写，不能只按 author 取
UPLOADER_FIELDS = ("author", "auther", "nickname", "name")
# 作者信息在有的端点里是对象：B站 user.name、快手 author.name
UPLOADER_OBJECT_FIELDS = ("user", "author", "auther")
COVER_FIELDS = ("cover", "imgurl", "pic", "image")
NAME_FIELDS = ("name", "nickname", "nickName")


class FallbackError(RuntimeError):
    """兜底解析不可用：没配 Key、Key 被拒、接口报错或没有可下载的直链。"""


@dataclass(frozen=True, slots=True)
class FallbackMedia:
    """兜底接口解析出的作品信息；`url` 是带时效签名的视频直链。"""

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
    """按别名顺序取第一个非空字符串字段。"""

    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pick_uploader(data: dict) -> str | None:
    direct = _first_text(data, UPLOADER_FIELDS)
    if direct:
        return direct
    # 作者可能是对象：B站 data.user.name、快手 data.author.name
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
    # 合集与多码率作品把直链放在列表里，取第一条即可用
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
    """把可能是时长的数字收敛成合理秒数；不像秒就返回 None。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < float(value) <= MAX_PLAUSIBLE_SECONDS else None


def _pick_duration(data: dict) -> float | None:
    """时长（秒）。接口只在部分平台返回，取值不合理时宁可当作没有。"""

    for field in ("duration", "videoDuration", "duration_seconds"):
        seconds = _plausible_seconds(data.get(field))
        if seconds is not None:
            return seconds
    # B站 把每集时长放在 videos[] 里，取的正是直链对应的那一集
    videos = data.get("videos")
    if isinstance(videos, list) and videos and isinstance(videos[0], dict):
        return _plausible_seconds(videos[0].get("duration"))
    return None


def parse_payload(payload: object) -> FallbackMedia:
    """把接口返回的 JSON 映射成 `FallbackMedia`；失败时抛出可读的错误。"""

    if not isinstance(payload, dict):
        raise FallbackError("兜底解析服务返回的内容无法识别")
    if str(payload.get("code")) != "200":
        message = _first_text(payload, ("error", "msg", "message")) or "兜底解析服务没能解析这条链接"
        raise FallbackError(message)

    data = payload.get("data")
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), None)
    if not isinstance(data, dict):
        raise FallbackError("兜底解析服务没有返回作品数据")

    url = _pick_url(data)
    if not url:
        raise FallbackError("兜底解析服务没有拿到视频直链，这条作品可能是图文或需要登录")

    title = _first_text(data, TITLE_FIELDS) or "未命名视频"
    description = _first_text(data, DESCRIPTION_FIELDS)
    return FallbackMedia(
        title=title[:MAX_TITLE_CHARS],
        url=url,
        uploader=_pick_uploader(data),
        thumbnail=_first_text(data, COVER_FIELDS),
        description=description[:MAX_DESCRIPTION_CHARS] if description else None,
        duration_seconds=_pick_duration(data),
    )


def _error_message(response: httpx.Response) -> str | None:
    """接口失败时优先用响应体里的说明（例如「小红书作品不可访问或需要登录」）。"""

    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return _first_text(payload, ("error", "message", "msg"))


def _resolve_at(path: str, url: str, api_key: str) -> FallbackMedia:
    headers = {"X-API-Key": api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
    try:
        response = httpx.get(
            f"{BASE_URL}{path}",
            params={"url": url},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        logger.info("兜底解析接口连接失败：%s", exc)
        raise FallbackError("兜底解析服务连接失败，请检查网络后重试") from exc

    message = _error_message(response)
    if response.status_code in (401, 403):
        # 401 是 Key 无效、403 按文档是余额/点数不足，都归到「先去设置页看 Key」
        raise FallbackError(
            f"兜底解析服务拒绝了这次请求（{message or 'API Key 无效'}）："
            "请在「设置」页确认 API Key 是否有效、额度是否充足"
        )
    if response.status_code >= 400:
        logger.info("兜底解析接口 %s 返回 HTTP %s：%s", path, response.status_code, message)
        raise FallbackError(message or f"兜底解析服务暂时不可用（HTTP {response.status_code}），请稍后重试")
    try:
        payload = response.json()
    except ValueError as exc:
        raise FallbackError("兜底解析服务返回的内容无法识别") from exc
    return parse_payload(payload)


def resolve(url: str, api_key: str, platform: str | None = None) -> FallbackMedia:
    """调用兜底接口解析作品；`api_key` 由调用方从设置里取出来。

    `platform` 用于挑选平台专属端点（B站 走聚合端点会 502），未知平台用聚合端点。
    """

    if not BASE_URL:
        # 正常不会走到这里：BASE_URL 有内置网关兜底，只有被外部改写成空串时才会命中。
        # 留这道防线是为了不发请求到空地址，报错里保留环境变量名方便定位。
        raise FallbackError(
            "兜底解析网关地址为空（内置网关未生效）：请检查 VTW_FALLBACK_API_BASE 环境变量"
        )
    paths = PLATFORM_PATHS.get(platform or "", DEFAULT_PATHS)
    first_error: FallbackError | None = None
    for path in paths:
        try:
            return _resolve_at(path, url, api_key)
        except FallbackError as exc:
            if first_error is None:
                first_error = exc
            logger.info("兜底解析 %s 未成功：%s", path, exc)
    # paths 至少有一项，走到这里必定已经失败过一次
    assert first_error is not None
    raise first_error


def download(media: FallbackMedia, workspace: Path) -> Path:
    """下载兜底直链到工作目录，返回视频文件路径。"""

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
            # CDN 出错时会以 200 + HTML 返回：直接存下来只会让 FFmpeg 报出难懂的错
            content_type = response.headers.get("content-type", "").lower()
            if content_type.startswith("text/") or "html" in content_type:
                raise FallbackError(
                    f"兜底直链返回的不是视频内容（{content_type or '未知类型'}），请稍后重试"
                )
            with target.open("wb") as file:
                for chunk in response.iter_bytes(1 << 16):
                    file.write(chunk)
    except httpx.HTTPError as exc:
        target.unlink(missing_ok=True)
        logger.info("兜底直链下载失败：%s", exc)
        raise FallbackError("下载兜底解析出的视频失败，请稍后重试") from exc

    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise FallbackError("下载兜底解析出的视频失败，请稍后重试")
    return target
