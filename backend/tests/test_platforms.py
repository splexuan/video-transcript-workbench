from app.application.platforms import detect_platform
from app.domain import Platform


def test_detects_supported_platforms() -> None:
    assert detect_platform("url", "https://www.bilibili.com/video/BV1xx") == Platform.BILIBILI
    assert detect_platform("url", "https://v.douyin.com/abc") == Platform.DOUYIN
    assert detect_platform("url", "https://www.xiaohongshu.com/explore/abc") == Platform.XIAOHONGSHU
    assert detect_platform("file", "D:/media/example.mp4") == Platform.LOCAL


def test_detects_kuaishou_hosts() -> None:
    """快手的分享短链、移动端分享页、老域名都要认出来。"""

    assert detect_platform("url", "https://v.kuaishou.com/example") == Platform.KUAISHOU
    assert detect_platform(
        "url", "https://c.kuaishou.com/fw/photo/3x5z75g4ym6vj3q"
    ) == Platform.KUAISHOU
    assert detect_platform(
        "url", "https://www.kuaishou.com/short-video/3x5z75g4ym6vj3q"
    ) == Platform.KUAISHOU
    assert detect_platform("url", "https://www.kuaishou.cn/short-video/3xabc") == Platform.KUAISHOU


def test_unknown_url_is_explicit() -> None:
    assert detect_platform("url", "https://example.com/video") == Platform.UNKNOWN

