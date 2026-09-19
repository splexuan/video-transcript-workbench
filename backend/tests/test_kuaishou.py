"""快手分享页解析：不依赖网络，用裁剪过的真实页面片段验证纯函数。"""

from __future__ import annotations

import pytest

from app.infrastructure.kuaishou import (
    REQUEST_HEADERS,
    KuaishouError,
    parse_duration,
    parse_media,
    quality_rank,
    rank_candidates,
)

# 按真实分享页裁剪的样本：作品数据、推广弹窗、多个 CDN 的直链都在里面。
# 真实页面里推广配置和作品数据隔着大段内容，这里用占位内容保持同样的间距。
PROMO_BLOCK = (
    '<script>window.__CONF__={"strongMessage":{"default":'
    '{"title":"去快手享超清画质","showCondition":60000,"duration":30000}}}</script>'
)
SAMPLE_HTML = (
    PROMO_BLOCK
    + "<script>/* 页面中的其它配置 */</script>" * 8
    + '<script>{"photoType":"VIDEO",'
    '"caption":"是谁拥有了一米长的玫瑰花呀 #一束花的仪式感",'
    '"userName":"好想吃巧克力",'
    '"coverUrls":[{"cdn":"p5.a.yximgs.com",'
    '"url":"https://p5.a.yximgs.com/upic/cover_B82a8b12.jpg?bp=10000"}],'
    '"adaptationSet":[{"avgEntropy":12.447}],"duration":6500}</script>'
    '<script>var a="https://hwmov.a.yximgs.com/upic/2024/11/21/00/photo_b_B64ce40.mp4";'
    'var b="https://tymov2.a.kwimgs.com/upic/2024/11/21/00/photo_b_B64ce40.mp4";'
    'var c="https://x.djvod.ndcimgs.com/bs2/photo-video-mz/5239938_v6HighV5.mp4";</script>'
)


def test_requests_use_mobile_user_agent() -> None:
    """分享页只在移动端 UA 下直出直链，PC UA 拿到的是空壳 SPA。

    这条前提一旦被改坏，解析会静默地一条直链都拿不到，所以固定住它。
    """

    agent = REQUEST_HEADERS["User-Agent"]

    assert "iPhone" in agent
    assert "Mobile" in agent


def test_parse_media_reads_caption_author_and_links() -> None:
    media = parse_media(SAMPLE_HTML)

    assert media.title == "是谁拥有了一米长的玫瑰花呀 #一束花的仪式感"
    assert media.uploader == "好想吃巧克力"
    assert media.duration_seconds == pytest.approx(6.5)
    assert media.candidates
    assert all(candidate.endswith(".mp4") for candidate in media.candidates)
    # 平台标识要跟着任务走，首页和编辑页都靠它显示来源
    assert media.to_media_info().platform == "kuaishou"


def test_parse_media_prefers_clearer_variants() -> None:
    """同一作品有多个清晰度时，把更清晰的排在前面。"""

    media = parse_media(SAMPLE_HTML)

    assert "v6HighV5" in media.candidates[0]


def test_parse_duration_skips_promo_popup() -> None:
    """页面里的推广弹窗也带 duration，不能把它当成作品时长。"""

    html = (
        '{"strongMessage":{"default":{"showCondition":60000,"duration":30000}}}'
        + "x" * 200  # 真实页面里这两处相距很远，中间隔着大量无关内容
        + '{"duration":17157}{"duration":17033}'
    )

    assert parse_duration(html) == pytest.approx(17.033)


def test_parse_duration_returns_none_when_absent() -> None:
    # 取不到就交给后续识别补真实时长，不要猜一个值
    assert parse_duration("<html></html>") is None


def test_parse_media_rejects_picture_post() -> None:
    """图集作品没有音频，要直接说清楚而不是让后续流程空转。"""

    with pytest.raises(KuaishouError, match="图集"):
        parse_media('{"photoType":"PICTURE","caption":"九宫格"}')
    with pytest.raises(KuaishouError, match="图集"):
        parse_media('{"photoType":"VERTICAL","caption":"长图"}')


def test_parse_media_reports_missing_video() -> None:
    with pytest.raises(KuaishouError, match="分享链接"):
        parse_media('{"photoType":"VIDEO","caption":"标题"}')


def test_quality_rank_orders_variants() -> None:
    ultra = quality_rank("https://x.com/upic/1_v6UltraV5.mp4")
    high = quality_rank("https://x.com/upic/1_v6HighV5.mp4")
    plain = quality_rank("https://x.com/upic/1.mp4")
    standard = quality_rank("https://x.com/upic/1_b_abc.mp4")

    assert ultra > high > plain > standard


def test_rank_candidates_dedupes_addresses_and_keeps_mirrors() -> None:
    """同一地址的不同签名只留一条；不同 CDN 的镜像保留下来当备选。"""

    ranked = rank_candidates(
        [
            "https://a.com/upic/one_b_x.mp4",
            "https://a.com/upic/one_b_x.mp4?tag=1",
            "https://b.com/upic/one_v6UltraV5.mp4",
            "https://b.com/upic/one_v6UltraV5.mp4?tag=2",
        ]
    )

    assert ranked == [
        "https://b.com/upic/one_v6UltraV5.mp4",
        "https://a.com/upic/one_b_x.mp4",
    ]


def test_rank_candidates_limits_tries() -> None:
    links = [f"https://cdn{index}.com/upic/v{index}.mp4" for index in range(9)]

    assert len(rank_candidates(links)) == 4


def test_parse_media_reads_cover_and_keeps_full_caption() -> None:
    """封面是对象数组，取第一条的 url；完整文案同时留给作品介绍。"""

    media = parse_media(SAMPLE_HTML)

    assert media.thumbnail == "https://p5.a.yximgs.com/upic/cover_B82a8b12.jpg?bp=10000"
    assert media.description == "是谁拥有了一米长的玫瑰花呀 #一束花的仪式感"
    # 元信息要跟着任务一路带到文档里
    info = media.to_media_info()
    assert info.thumbnail == media.thumbnail
    assert info.description == media.description
    assert info.uploader == "好想吃巧克力"


def test_parse_media_without_cover_is_fine() -> None:
    """没有封面只是少张图，不影响解析。"""

    media = parse_media(
        '{"photoType":"VIDEO","caption":"没有封面"}"https://x.com/upic/photo_b_abc.mp4"'
    )

    assert media.thumbnail is None
    assert media.title == "没有封面"
