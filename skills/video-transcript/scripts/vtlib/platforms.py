"""链接清洗与平台识别。"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# 分享文案常把链接夹在文字中间；字符集按 RFC 3986 限定为 ASCII，
# 中文标点与空格会自然截断，避免把后面紧跟的中文一起并进来。
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
TRAILING_PUNCTUATION = "，。；、！？：,.;:!?"

PLATFORM_LABELS = {
    "bilibili": "B站",
    "douyin": "抖音",
    "kuaishou": "快手",
    "xiaohongshu": "小红书",
    "wechat": "视频号",
    "local": "本地文件",
}

SUPPORTED_PLATFORMS = {"bilibili", "douyin", "kuaishou", "xiaohongshu", "wechat"}
# 这些平台没有本机解析方案，只能走兜底接口
FALLBACK_ONLY_PLATFORMS = {"wechat"}
# 支持 Cookie 域名的平台
COOKIE_PLATFORMS = {"bilibili", "douyin", "xiaohongshu"}


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, "该平台")


def extract_source_url(raw: str) -> str:
    """从整段分享文案里取出链接；没有链接时原样返回，交给后续流程报错。"""

    text = raw.strip().strip("\"'“”‘’")
    match = URL_PATTERN.search(text)
    if match is None:
        return text
    return match.group(0).rstrip(TRAILING_PUNCTUATION)


def detect_platform(source: str) -> str:
    host = (urlparse(source).hostname or "").lower()
    if host.endswith(("bilibili.com", "b23.tv")):
        return "bilibili"
    if host.endswith(("douyin.com", "iesdouyin.com")):
        return "douyin"
    if host.endswith(("xiaohongshu.com", "xhslink.com", "xhslink.cn")):
        return "xiaohongshu"
    if host.endswith(("kuaishou.com", "kuaishou.cn")):
        return "kuaishou"
    if host.endswith(("weixin.qq.com", "channels.weixin.qq.com")):
        return "wechat"
    return "unknown"
