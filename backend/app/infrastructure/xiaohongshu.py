"""小红书：解析分享链接并取得视频文件。

桌面 UA 下游客访问作品页会被 302 到登录页，但**移动端 UA 的游客**能拿到
完整的 `__INITIAL_STATE__`——笔记类型、视频流、时长都在（登录弹窗只是
页面装饰，数据早已服务端渲染好）。所以这里和快手一样走移动端分享页。

要点：
- 必须用手机 UA；桌面 UA 游客会被要求登录。
- 用户在「平台连接」页配了凭据时优先带凭据请求：同样是单次请求，但登录态
  命中风控的概率低得多；凭据不可用（未配置 / 已失效）会自动退回纯游客。
- `media.stream` 按编码分组（h264/h265/h266/av1），每组是变体数组；
  h264 兼容性最好优先选，组内取体积（size）最大的变体。
- 笔记页的 title 常为空，用 desc 首行（去掉 #话题# 标记）兜底。
- 直链带时效签名，只能现用现取，不能跨任务缓存。
- 页面结构由平台控制，随时可能变化；解析失败时给出可读提示。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.domain import MediaInfo
from app.infrastructure.browser_login import BrowserLoginError, fetch_note_via_browser
from app.infrastructure.credential_store import COOKIE_DOMAINS, read_netscape_cookies

logger = logging.getLogger(__name__)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
REQUEST_HEADERS: dict[str, str] = {
    "User-Agent": MOBILE_UA,
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def request_headers(cookies_file: Path | None = None) -> dict[str, str]:
    """组装请求头；用户配了访问凭据就带上，登录态能明显降低被风控拦下的概率。"""

    headers = dict(REQUEST_HEADERS)
    cookies = read_netscape_cookies(cookies_file, COOKIE_DOMAINS["xiaohongshu"])
    if cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
    return headers

STATE_PATTERN = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.DOTALL)
# 分享文案里的话题标记，取标题时去掉
TOPIC_MARKER = re.compile(r"\s*#[^#\s]{0,40}\[话题\]#")

# 编码优先级：h264 兼容性最好；h265/h266/av1 也能被 FFmpeg 解码，作为回退
CODEC_PRIORITY = ("h264", "h265", "h266", "av1")

REQUEST_TIMEOUT = 25.0
DOWNLOAD_TIMEOUT = 300.0
MAX_CANDIDATES = 3
MAX_TITLE_CHARS = 120
# 作品介绍可能很长，落库前截断，与下载器链路的处理保持一致
MAX_DESCRIPTION_CHARS = 2000


class XiaohongshuError(RuntimeError):
    """小红书分享页解析或视频下载失败。"""


@dataclass(frozen=True)
class XiaohongshuMedia:
    title: str
    uploader: str | None
    duration_seconds: float | None
    thumbnail: str | None
    description: str | None
    candidates: list[str]

    def to_media_info(self) -> MediaInfo:
        return MediaInfo(
            title=self.title,
            platform="xiaohongshu",
            duration_seconds=self.duration_seconds,
            uploader=self.uploader,
            thumbnail=self.thumbnail,
            description=self.description,
        )


def _pick_title(note: dict) -> str:
    """标题兜底链：title → desc 正文 → 第一个话题名 → 「小红书笔记」。

    有些创作者发布时只贴话题不写字，desc 去掉话题后一个字都不剩，
    这时用第一个话题名（通常是创作者认为最重要的词）总比通用占位强。
    """

    title = str(note.get("title") or "").strip()
    if title:
        return title[:MAX_TITLE_CHARS]
    description = str(note.get("desc") or "").strip()
    if description:
        cleaned = TOPIC_MARKER.sub("", description)
        first_line = next((line.strip() for line in cleaned.splitlines() if line.strip()), "")
        if first_line:
            return first_line[:MAX_TITLE_CHARS]
        topics = re.findall(r"#([^#\s]{1,40})\[话题\]#", description)
        if topics:
            return topics[0][:MAX_TITLE_CHARS]
    return "小红书笔记"


def _stream_candidates(note: dict) -> list[str]:
    """按编码优先级收集可用的视频直链；组内取码率最大的变体。"""

    stream = (
        ((note.get("video") or {}).get("media") or {}).get("stream") or {}
    )
    candidates: list[str] = []
    for codec in CODEC_PRIORITY:
        variants = [item for item in (stream.get(codec) or []) if isinstance(item, dict)]
        if not variants:
            continue
        # 清晰度最高的变体音轨通常最完整（size 是文件字节数）
        best = max(variants, key=lambda item: int(item.get("size") or 0))
        url = str(best.get("masterUrl") or best.get("videoUrl") or "")
        if url.startswith("http"):
            candidates.append(url)
    return candidates[:MAX_CANDIDATES]


def parse_note(state: dict) -> XiaohongshuMedia:
    """从 __INITIAL_STATE__ 里取出笔记信息与可用的视频直链。"""

    note = ((state.get("noteData") or {}).get("data") or {}).get("noteData") or {}
    if not note:
        raise XiaohongshuError(
            "没能从小红书页面里读到笔记内容：链接可能已失效，或作品被风控拦截。"
            "请在浏览器里确认能打开后再试"
        )
    if note.get("type") != "video":
        raise XiaohongshuError("这是图文笔记，没有音频可以识别，请改用视频笔记或导入本地文件")

    candidates = _stream_candidates(note)
    if not candidates:
        raise XiaohongshuError(
            "没能从这条小红书内容里取到视频：它可能已被删除或仅登录可见。"
            "请在「平台连接」页配置 Cookie 后重试"
        )

    video = note.get("video") or {}
    media = video.get("media") or {}
    variants = [item for group in (media.get("stream") or {}).values() for item in (group or []) if isinstance(item, dict)]
    # 时长跟着所选变体走（码率最大的那个），不同清晰度的 duration 是同一音频
    best_variant = max(variants, key=lambda item: int(item.get("size") or 0)) if variants else None
    duration_ms = int(best_variant["duration"]) if best_variant and best_variant.get("duration") else None

    frames = note.get("imageList") or []
    cover = str(frames[0].get("urlDefault") or frames[0].get("url") or "") or None if frames else None
    user = note.get("user") or {}

    # 分享页用 nickName，网页端结构用 nickname，两种写法都认
    uploader = str(user.get("nickName") or user.get("nickname") or "").strip() or None
    description = str(note.get("desc") or "").strip()[:MAX_DESCRIPTION_CHARS] or None
    return XiaohongshuMedia(
        title=_pick_title(note),
        uploader=uploader,
        duration_seconds=duration_ms / 1000 if duration_ms else None,
        thumbnail=cover,
        description=description,
        candidates=candidates,
    )


def _load_state(html: str) -> dict:
    match = STATE_PATTERN.search(html)
    if match is None:
        raise XiaohongshuError("没能从小红书页面里读到笔记内容，请稍后重试")
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        # 页面内联 JSON 里的 undefined 不是合法 JSON 值。只在直接解析失败时才做替换：
        # 否则正文里恰好出现 undefined 这个词也会被改成 null。
        return json.loads(re.sub(r"\bundefined\b", "null", raw))
    except json.JSONDecodeError as exc:
        logger.info("小红书 __INITIAL_STATE__ 解析失败：%s", exc)
        raise XiaohongshuError("小红书页面结构发生变化，暂时无法解析，请稍后重试") from exc


def fetch_share_page(url: str, cookies_file: Path | None = None) -> tuple[str, str]:
    """读取分享页 HTML，返回 (页面内容, 最终地址)；短链由移动 UA 自动跟随跳转。

    `cookies_file` 是任务期间临时落盘的凭据文件，带上它就是用登录态访问。
    """

    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers=request_headers(cookies_file),
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text, str(response.url)
    except httpx.HTTPError as exc:
        logger.info("读取小红书分享页失败：%s", exc)
        raise XiaohongshuError("读取小红书分享页失败，请检查网络后重试") from exc


def resolve_media(url: str, cookies_file: Path | None = None) -> XiaohongshuMedia:
    html, final_url = fetch_share_page(url, cookies_file)
    # xsec_token 与分享会话绑定，直连命中风控时会被 302 到安全拦截页。
    # 这是概率性的：同一批链接有的能过有的被拦，登录态通常能解决。
    if "/404/sec_" in final_url:
        raise XiaohongshuError(
            "这条笔记触发了小红书的风控拦截，当前身份暂时无法访问。"
            "请在浏览器里打开这条链接确认可见，并在「平台连接」页导入你浏览器的 "
            "Cookie 后重试（登录态可以显著提高成功率）"
        )
    # 凭据失效、或这条笔记本身仅登录可见时，都会被重定向到登录页；
    # 这里提前识别，避免把登录页当成笔记页去解析。
    if "/login" in final_url:
        raise XiaohongshuError(
            "这条笔记需要登录才能查看：当前访问凭据已失效或权限不足。"
            "请在「平台连接」页重新获取凭据，或用「浏览器登录」后再试一次"
        )
    return parse_note(_load_state(html))


def resolve_with_fallback(url: str, cookies_file: Path | None = None) -> XiaohongshuMedia:
    """HTTP 解析失败时，用本机浏览器打开页面再读（等于用户手动操作的自动化版）。

    配了凭据就先带凭据请求（同样是单次请求，命中风控的概率低得多），失败再退回
    纯游客；两条 HTTP 路径都不行才动用本机浏览器——那里有真实指纹和资料目录中
    已保存的登录态，风控基本不拦，登录弹窗由注入脚本移除，数据早已渲染在
    `__INITIAL_STATE__` 里。浏览器也失败时抛出原始的 HTTP 解析错误（文案更可读）。
    """

    attempts = [cookies_file, None] if cookies_file is not None else [None]
    http_error: XiaohongshuError | None = None
    for attempt in attempts:
        try:
            return resolve_media(url, attempt)
        except XiaohongshuError as exc:
            if http_error is None:
                http_error = exc
    try:
        note = fetch_note_via_browser("xiaohongshu", url)
        logger.info("HTTP 解析均失败，已通过本机浏览器取回笔记 %s", note.get("noteId"))
        return parse_note({"noteData": {"data": {"noteData": note}}})
    except (BrowserLoginError, XiaohongshuError, ValueError) as browser_error:
        logger.info("浏览器抓取小红书笔记失败：%s", browser_error)
        assert http_error is not None  # attempts 至少有一项，失败时必然被赋值
        raise http_error from browser_error


def _download(url: str, target: Path) -> None:
    with httpx.stream(
        "GET",
        url,
        headers=REQUEST_HEADERS,
        timeout=DOWNLOAD_TIMEOUT,
        follow_redirects=True,
    ) as response:
        response.raise_for_status()
        # 风控页/错误页会以 200 + HTML 返回：直接存下来会让 FFmpeg 报出难懂的错，
        # 这里提前识别，交给调用方换下一个直链。
        content_type = response.headers.get("content-type", "").lower()
        if content_type.startswith("text/") or "html" in content_type:
            raise httpx.HTTPError(f"直链返回的不是视频内容（{content_type or '未知类型'}）")
        with target.open("wb") as file:
            for chunk in response.iter_bytes(1 << 16):
                file.write(chunk)


def download_media(
    url: str,
    workspace: Path,
    media: XiaohongshuMedia | None = None,
) -> Path:
    """下载视频文件；某个直链取不到时换下一个编码或清晰度。

    `media` 是调用方已经解析好的结果：同一次任务里解析阶段刚取过直链，
    直接复用可以少请求一次分享页（也少一次被风控拦掉的机会）。
    """

    resolved = media or resolve_with_fallback(url)
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

    raise XiaohongshuError("下载小红书视频失败，请稍后重试或改用分享链接") from last_error
