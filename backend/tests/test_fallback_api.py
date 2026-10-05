"""兜底解析接口的纯函数测试（不联网）。"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest

from app.infrastructure import fallback_api


@pytest.fixture(autouse=True)
def _gateway(monkeypatch) -> None:
    """兜底网关地址由使用者通过环境变量指定，测试里统一注入一个假地址。"""

    monkeypatch.setattr(fallback_api, "BASE_URL", "https://gateway.test")


class FakeResponse:
    """最小响应对象：只实现被测代码用到的方法。"""

    def __init__(self, status_code: int, payload: object = None, *, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


class FakeStream:
    """最小流式响应对象。"""

    def __init__(self, content_type: str, chunks: list[bytes]) -> None:
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, _size: int):
        return iter(self._chunks)


def test_aggregate_path_matches_documented_gateway() -> None:
    """聚合端点的路径来自接口文档（API Code: svparse），别写成别的名字。"""

    assert fallback_api.AGGREGATE_PATH == "/api/svparse"


def test_resolve_requires_gateway_configuration(monkeypatch) -> None:
    """没有配置网关地址时给出可操作的提示，而不是发请求到一个空地址。"""

    monkeypatch.setattr(fallback_api, "BASE_URL", "")
    with pytest.raises(fallback_api.FallbackError, match="VTW_FALLBACK_API_BASE"):
        fallback_api.resolve("https://www.bilibili.com/video/BV1x", "sk-test", "bilibili")


def test_parse_payload_maps_documented_fields() -> None:
    media = fallback_api.parse_payload(
        {
            "code": 200,
            "msg": "解析成功",
            "data": {
                "author": "牛老师",
                "title": "步数打卡",
                "cover": "https://cdn.example/cover.webp",
                "url": "https://cdn.example/video.mp4",
            },
        }
    )

    assert media.title == "步数打卡"
    assert media.uploader == "牛老师"
    assert media.thumbnail == "https://cdn.example/cover.webp"
    assert media.url == "https://cdn.example/video.mp4"
    assert media.duration_seconds is None


def test_parse_payload_reads_nested_author_object() -> None:
    """快手端点把作者放在对象里（author.name），且直链可能是备选列表里的第一条。"""

    media = fallback_api.parse_payload(
        {
            "code": 200,
            "msg": "解析成功-esa",
            "data": {
                "type": "video",
                "title": "是谁拥有了一米长的玫瑰花呀",
                "desc": "是谁拥有了一米长的玫瑰花呀 #一束花的仪式感",
                "duration": 6,
                "author": {"name": "好想吃巧克力", "id": 1737389746},
                "video_backup": [{"url": "https://cdn.example/backup.mp4", "quality": "720p"}],
            },
        }
    )

    assert media.uploader == "好想吃巧克力"
    assert media.duration_seconds == 6.0
    assert media.description == "是谁拥有了一米长的玫瑰花呀 #一束花的仪式感"
    assert media.url == "https://cdn.example/backup.mp4"


def test_parse_payload_reads_bilibili_fields() -> None:
    """B站端点的作者字段拼成了 `auther`，时长在 videos[0] 里（秒）。"""

    media = fallback_api.parse_payload(
        {
            "code": 200,
            "msg": "解析成功！",
            "data": {
                "title": "专访",
                "auther": "温柔JUNZ",
                "user": {"name": "温柔JUNZ"},
                "description": "正文",
                "url": "https://cdn.example/v.mp4",
                "videos": [{"index": 1, "duration": 1004, "url": "https://cdn.example/1.mp4"}],
            },
        }
    )

    assert media.uploader == "温柔JUNZ"
    assert media.url == "https://cdn.example/v.mp4"
    assert media.duration_seconds == 1004.0


def test_parse_payload_accepts_field_aliases() -> None:
    """接口文档与实际返回的字段名不完全一致，封面 imgurl / 直链 video_url 都要认。"""

    media = fallback_api.parse_payload(
        {
            "code": 200,
            "data": {
                "title": "标题",
                "imgurl": "https://cdn.example/c.jpg",
                "video_url": "https://cdn.example/v.mp4",
                "desc": "正文",
            },
        }
    )

    assert media.thumbnail == "https://cdn.example/c.jpg"
    assert media.url == "https://cdn.example/v.mp4"
    assert media.description == "正文"


def test_parse_payload_ignores_implausible_duration() -> None:
    """时长超出合理范围（多半是把毫秒当秒）时宁可当作没有。"""

    media = fallback_api.parse_payload(
        {"code": 200, "data": {"title": "标题", "url": "https://cdn.example/v.mp4", "duration": 17159000}}
    )

    assert media.duration_seconds is None


def test_parse_payload_surfaces_api_message() -> None:
    with pytest.raises(fallback_api.FallbackError, match="链接已经失效"):
        fallback_api.parse_payload({"code": 400, "msg": "链接已经失效"})


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 200, "msg": "解析成功", "data": {}},
        {"code": 200, "data": None},
        {"code": 200, "data": {"title": "只有标题"}},
        "不是 JSON 对象",
    ],
)
def test_parse_payload_rejects_bad_payloads(payload: object) -> None:
    with pytest.raises(fallback_api.FallbackError):
        fallback_api.parse_payload(payload)


def test_resolve_sends_api_key_and_parses(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        seen["url"] = url
        seen["params"] = kwargs.get("params")
        seen["headers"] = kwargs.get("headers")
        return FakeResponse(200, {"code": 200, "data": {"title": "标题", "url": "https://cdn.example/v.mp4"}})

    monkeypatch.setattr(fallback_api.httpx, "get", fake_get)

    media = fallback_api.resolve("https://www.bilibili.com/video/BV1x", "sk-test", "bilibili")

    assert media.url == "https://cdn.example/v.mp4"
    assert seen["url"] == f"{fallback_api.BASE_URL}/api/bilibili"
    assert seen["params"] == {"url": "https://www.bilibili.com/video/BV1x"}
    assert seen["headers"]["X-API-Key"] == "sk-test"  # type: ignore[index]


def test_resolve_picks_path_by_platform(monkeypatch) -> None:
    """各平台用各自的专属端点，未知平台用聚合端点。"""

    seen: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> FakeResponse:
        seen.append(url)
        return FakeResponse(200, {"code": 200, "data": {"title": "标题", "url": "https://cdn.example/v.mp4"}})

    monkeypatch.setattr(fallback_api.httpx, "get", fake_get)

    fallback_api.resolve("https://www.bilibili.com/video/BV1x", "sk-test", "bilibili")
    fallback_api.resolve("https://www.douyin.com/video/1", "sk-test", "douyin")
    fallback_api.resolve("https://v.kuaishou.com/abc", "sk-test", "kuaishou")
    fallback_api.resolve("https://weixin.qq.com/sph/abc", "sk-test", "wechat")
    fallback_api.resolve("https://example.com/v", "sk-test")

    assert seen == [
        f"{fallback_api.BASE_URL}/api/bilibili",
        f"{fallback_api.BASE_URL}/api/dyjx",
        f"{fallback_api.BASE_URL}/api/ksjx",
        f"{fallback_api.BASE_URL}/api/wxsph",
        f"{fallback_api.BASE_URL}{fallback_api.AGGREGATE_PATH}",
    ]


def test_resolve_retries_next_path(monkeypatch) -> None:
    """专属端点失败时继续试下一个端点（实测 B站 走聚合端点会 502）。"""

    seen: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> FakeResponse:
        seen.append(url)
        if url.endswith("/api/bilibili"):
            return FakeResponse(502, {"code": -1, "error": "请求失败-iga"})
        return FakeResponse(200, {"code": 200, "data": {"title": "标题", "url": "https://cdn.example/v.mp4"}})

    monkeypatch.setattr(fallback_api.httpx, "get", fake_get)

    media = fallback_api.resolve("https://www.bilibili.com/video/BV1x", "sk-test", "bilibili")

    assert media.title == "标题"
    assert seen == [f"{fallback_api.BASE_URL}/api/bilibili", f"{fallback_api.BASE_URL}/api/svparse"]


def test_resolve_raises_first_error_when_all_paths_fail(monkeypatch) -> None:
    def fake_get(_url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse(502, {"code": -1, "error": "上游失败了"})

    monkeypatch.setattr(fallback_api.httpx, "get", fake_get)

    with pytest.raises(fallback_api.FallbackError, match="上游失败了"):
        fallback_api.resolve("https://www.bilibili.com/video/BV1x", "sk-test", "bilibili")


def test_resolve_surfaces_endpoint_error_message(monkeypatch) -> None:
    """接口失败时用响应体里的说明，而不是干巴巴的 HTTP 502。"""

    monkeypatch.setattr(
        fallback_api.httpx,
        "get",
        lambda *_args, **_kwargs: FakeResponse(
            502, {"code": -1, "data": None, "error": "小红书作品不可访问或需要登录-eo"}
        ),
    )

    with pytest.raises(fallback_api.FallbackError, match="小红书作品不可访问或需要登录"):
        fallback_api.resolve("https://www.xiaohongshu.com/explore/abc", "sk-test", "xiaohongshu")


@pytest.mark.parametrize("status", [401, 403])
def test_resolve_reports_rejected_key(monkeypatch, status: int) -> None:
    monkeypatch.setattr(
        fallback_api.httpx, "get", lambda *_args, **_kwargs: FakeResponse(status, {"code": -1, "error": "API Key无效"})
    )

    with pytest.raises(fallback_api.FallbackError, match="API Key"):
        fallback_api.resolve("https://www.douyin.com/video/1", "sk-bad", "douyin")


def test_resolve_reports_server_error_without_body(monkeypatch) -> None:
    monkeypatch.setattr(fallback_api.httpx, "get", lambda *_args, **_kwargs: FakeResponse(502, text="bad gateway"))

    with pytest.raises(fallback_api.FallbackError, match="502"):
        fallback_api.resolve("https://www.douyin.com/video/1", "sk-test", "douyin")


def test_resolve_reports_network_failure(monkeypatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise fallback_api.httpx.ConnectError("boom")

    monkeypatch.setattr(fallback_api.httpx, "get", boom)

    with pytest.raises(fallback_api.FallbackError, match="连接失败"):
        fallback_api.resolve("https://www.douyin.com/video/1", "sk-test", "douyin")


def test_download_writes_video_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        fallback_api.httpx,
        "stream",
        lambda *_args, **_kwargs: nullcontext(FakeStream("video/mp4", [b"mp4", b"data"])),
    )
    media = fallback_api.FallbackMedia(title="标题", url="https://cdn.example/v.mp4")

    target = fallback_api.download(media, tmp_path)

    assert target == tmp_path / "source.mp4"
    assert target.read_bytes() == b"mp4data"


def test_download_rejects_html_error_page(monkeypatch, tmp_path: Path) -> None:
    """CDN 出错时会以 200 + HTML 返回，不能把网页当视频存下来。"""

    monkeypatch.setattr(
        fallback_api.httpx,
        "stream",
        lambda *_args, **_kwargs: nullcontext(FakeStream("text/html; charset=utf-8", [b"<html>403</html>"])),
    )
    media = fallback_api.FallbackMedia(title="标题", url="https://cdn.example/bad.mp4")

    with pytest.raises(fallback_api.FallbackError, match="不是视频内容"):
        fallback_api.download(media, tmp_path)

    assert not (tmp_path / "source.mp4").exists()


def test_download_cleans_up_after_http_error(monkeypatch, tmp_path: Path) -> None:
    class BrokenStream(FakeStream):
        def raise_for_status(self) -> None:
            raise fallback_api.httpx.HTTPStatusError("403", request=None, response=None)  # type: ignore[arg-type]

    monkeypatch.setattr(
        fallback_api.httpx, "stream", lambda *_args, **_kwargs: nullcontext(BrokenStream("video/mp4", []))
    )
    media = fallback_api.FallbackMedia(title="标题", url="https://cdn.example/v.mp4")

    with pytest.raises(fallback_api.FallbackError, match="下载兜底解析出的视频失败"):
        fallback_api.download(media, tmp_path)

    assert not (tmp_path / "source.mp4").exists()


def test_to_media_info_carries_metadata() -> None:
    media = fallback_api.FallbackMedia(
        title="标题",
        url="https://cdn.example/v.mp4",
        uploader="作者",
        thumbnail="https://cdn.example/c.jpg",
        description="正文",
        duration_seconds=12.0,
    )

    info = media.to_media_info("xiaohongshu")

    assert (info.title, info.platform, info.uploader, info.description) == ("标题", "xiaohongshu", "作者", "正文")
    assert info.duration_seconds == 12.0
    assert info.thumbnail == "https://cdn.example/c.jpg"
