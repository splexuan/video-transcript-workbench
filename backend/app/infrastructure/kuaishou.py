"""快手：解析分享链接并取得视频文件。

yt-dlp 没有快手 extractor（内置实现已移除），站内数据接口又带 `__NS_sig3`
签名风控，所以这里走平台自己的分享页：它在**移动端 User-Agent** 下直出
服务端渲染的 HTML，内嵌多条 mp4 直链，不需要登录态、Cookie 或 Referer。

要点：
- 必须用手机浏览器 UA。PC UA 拿到的是空壳 SPA（一个直链都没有）。
- 同一文件分布在多个 CDN 域名上，按清晰度排序、逐个尝试，提升成功率。
- 直链带时效签名，只能现用现取，不能缓存。
- 分享页结构由平台控制，随时可能变化；解析失败时给出可读提示。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.domain import MediaInfo

logger = logging.getLogger(__name__)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
REQUEST_HEADERS: dict[str, str] = {
    "User-Agent": MOBILE_UA,
    "Accept-Language": "zh-CN,zh;q=0.9",
}

MEDIA_PATTERN = re.compile(r"""https?://[^\s"'<>\\]+?\.mp4(?:\?[^\s"'<>\\]+)?""")
CAPTION_PATTERN = re.compile(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)"')
# 封面是对象数组：{"cdn": "p5.a.yximgs.com", "url": "https://..."}
COVER_PATTERN = re.compile(r'"coverUrls"\s*:\s*\[\s*\{[^{}]*?"url"\s*:\s*"([^"]+)"')
AUTHOR_PATTERN = re.compile(r'"userName"\s*:\s*"((?:[^"\\]|\\.)*)"')
PHOTO_TYPE_PATTERN = re.compile(r'"photoType"\s*:\s*"([^"]*)"')
DURATION_PATTERN = re.compile(r'"duration"\s*:\s*(\d+)')
# 页面里的推广弹窗也带一个 duration（弹窗展示时长），它紧挨着 showCondition。
# 只往左看一小段：窗口开大了会把远处的作品时长也一起误判掉。
PROMO_HINTS = ("strongMessage", "showCondition")
PROMO_WINDOW = 80

REQUEST_TIMEOUT = 25.0
# 短视频体积不大，留出余量但不至于卡住任务
DOWNLOAD_TIMEOUT = 300.0
MAX_CANDIDATES = 4
MAX_TITLE_CHARS = 120


class KuaishouError(RuntimeError):
    """快手分享页解析或视频下载失败。"""


@dataclass(frozen=True)
class KuaishouMedia:
    title: str
    uploader: str | None
    duration_seconds: float | None
    thumbnail: str | None
    description: str | None
    candidates: list[str]

    def to_media_info(self) -> MediaInfo:
        return MediaInfo(
            title=self.title,
            platform="kuaishou",
            duration_seconds=self.duration_seconds,
            uploader=self.uploader,
            thumbnail=self.thumbnail,
            description=self.description,
        )


def _unescape(text: str) -> str:
    """把 JSON 串里的转义还原成可读文本。"""

    return text.replace('\\"', '"').replace("\\/", "/").replace("\\n", " ").strip()


def quality_rank(url: str) -> int:
    """直链清晰度排序用的权重：超清 > 高清 > 其它 > 标清。

    画面清晰度对提取音频没有直接影响，但更高清晰度的版本音轨更完整，
    所以优先选它；只有标清时也能正常转写。
    """

    name = url.split("?")[0].rsplit("/", 1)[-1].lower()
    if "ultra" in name:
        return 3
    if "high" in name:
        return 2
    if "_b_" in name:
        return 0
    return 1


def rank_candidates(links: list[str]) -> list[str]:
    """按清晰度排序并去掉重复地址（同一地址会带不同的签名参数）。"""

    unique: list[str] = []
    seen: set[str] = set()
    for link in links:
        base = link.split("?")[0]
        if base in seen:
            continue
        seen.add(base)
        unique.append(link)
    unique.sort(key=quality_rank, reverse=True)
    return unique[:MAX_CANDIDATES]


def parse_duration(html: str) -> float | None:
    """作品时长（秒）；取不到就返回 None，后续识别会补上真实时长。"""

    values: list[int] = []
    for match in DURATION_PATTERN.finditer(html):
        before = html[max(0, match.start() - PROMO_WINDOW) : match.start()]
        if any(hint in before for hint in PROMO_HINTS):
            continue
        values.append(int(match.group(1)))
    if not values:
        return None
    return min(values) / 1000


def parse_media(html: str) -> KuaishouMedia:
    """从分享页 HTML 里取出作品信息与可用的 mp4 直链。"""

    photo_type = PHOTO_TYPE_PATTERN.search(html)
    if photo_type is not None and photo_type.group(1).upper() != "VIDEO":
        raise KuaishouError("这是图集作品，没有音频可以识别，请改用视频作品或导入本地文件")

    candidates = rank_candidates(MEDIA_PATTERN.findall(html))
    if not candidates:
        raise KuaishouError(
            "没能从这条快手内容里取到视频：链接可能已失效、作品已删除，"
            "或它不是公开作品。请改用分享链接或导入本地文件"
        )

    caption = CAPTION_PATTERN.search(html)
    text = _unescape(caption.group(1)) if caption else ""
    author = AUTHOR_PATTERN.search(html)
    cover = COVER_PATTERN.search(html)

    return KuaishouMedia(
        # 标题保持一行放得下，完整文案另存为作品介绍
        title=(text or "未命名视频")[:MAX_TITLE_CHARS],
        uploader=_unescape(author.group(1)) if author else None,
        duration_seconds=parse_duration(html),
        thumbnail=_unescape(cover.group(1)) if cover else None,
        description=text or None,
        candidates=candidates,
    )


def fetch_share_page(url: str) -> str:
    """读取分享页 HTML；短链由 HTTP 客户端自动跟随跳转。"""

    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers=REQUEST_HEADERS,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        logger.info("读取快手分享页失败：%s", exc)
        raise KuaishouError("读取快手分享页失败，请检查网络后重试") from exc


def resolve_media(url: str) -> KuaishouMedia:
    return parse_media(fetch_share_page(url))


def _download(url: str, target: Path) -> None:
    with httpx.stream(
        "GET",
        url,
        headers=REQUEST_HEADERS,
        timeout=DOWNLOAD_TIMEOUT,
        follow_redirects=True,
    ) as response:
        response.raise_for_status()
        with target.open("wb") as file:
            for chunk in response.iter_bytes(1 << 16):
                file.write(chunk)


def download_media(
    url: str,
    workspace: Path,
    media: KuaishouMedia | None = None,
) -> Path:
    """下载视频文件；某个直链取不到时换下一个清晰度或 CDN。

    `media` 是调用方已经解析好的结果：同一次任务里解析阶段刚取过直链，
    直接复用可以少请求一次分享页（也少一次被风控拦掉的机会）。
    """

    resolved = media or resolve_media(url)
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "source.mp4"
    last_error: Exception | None = None

    for candidate in resolved.candidates:
        try:
            _download(candidate, target)
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            target.unlink(missing_ok=True)
            continue
        if target.stat().st_size > 0:
            return target
        target.unlink(missing_ok=True)

    raise KuaishouError("下载快手视频失败，请稍后重试或改用分享链接") from last_error
