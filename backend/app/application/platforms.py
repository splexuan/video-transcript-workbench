import re
from urllib.parse import urlparse

from app.domain import Platform

# 分享文案常把链接夹在文字中间，这里把它取出来。
# 字符集按 RFC 3986 限定为 ASCII：中文标点（，！【】等）与空格都会让匹配停止，
# 否则会把「链接后面紧跟的中文」一起并进来。
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
TRAILING_PUNCTUATION = "，。；、！？：,.;:!?"


def extract_source_url(raw: str) -> str:
    """从粘贴内容里取出链接；没有链接时原样返回，交给后续流程报错。"""

    text = raw.strip()
    match = URL_PATTERN.search(text)
    if match is None:
        return text
    return match.group(0).rstrip(TRAILING_PUNCTUATION)


def detect_platform(source_type: str, source: str) -> Platform:
    if source_type == "file":
        return Platform.LOCAL

    host = (urlparse(source).hostname or "").lower()
    if host.endswith(("bilibili.com", "b23.tv")):
        return Platform.BILIBILI
    if host.endswith(("douyin.com", "iesdouyin.com")):
        return Platform.DOUYIN
    # 手机分享短链是 xhslink.cn，网页分享短链是 xhslink.com
    if host.endswith(("xiaohongshu.com", "xhslink.com", "xhslink.cn")):
        return Platform.XIAOHONGSHU
    # 分享短链 v.kuaishou.com、移动端分享页 c.kuaishou.com 都是它的子域
    if host.endswith(("kuaishou.com", "kuaishou.cn")):
        return Platform.KUAISHOU
    if host.endswith(("weixin.qq.com", "channels.weixin.qq.com")):
        return Platform.WECHAT
    return Platform.UNKNOWN

