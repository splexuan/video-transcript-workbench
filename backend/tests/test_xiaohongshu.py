"""小红书游客解析器的纯函数测试（不联网）。"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest

from app.infrastructure import xiaohongshu


def make_state(note: dict) -> dict:
    return {"noteData": {"data": {"noteData": note}}}


def test_parse_note_extracts_video_stream() -> None:
    state = make_state(
        {
            "noteId": "6f000000",
            "type": "video",
            "title": "",
            "desc": "#摆摊[话题]# #摆摊日记[话题]# 今天出摊啦",
            "user": {"nickname": "牛老师"},
            "imageList": [{"urlDefault": "https://sns-img.example/cover.jpg"}],
            "video": {
                "media": {
                    "videoDuration": None,
                    "stream": {
                        "h264": [
                            {"masterUrl": "https://cdn.example/low.mp4", "size": 1_000, "duration": 10_000},
                            {"masterUrl": "https://cdn.example/high.mp4", "size": 9_000, "duration": 250_753},
                        ],
                        "h265": [{"masterUrl": "https://cdn.example/h265.mp4", "size": 8_000}],
                    },
                }
            },
        }
    )

    media = xiaohongshu.parse_note(state)

    # h264 优先，组内取码率最大的变体；时长取毫秒转秒
    assert media.candidates[0] == "https://cdn.example/high.mp4"
    assert media.duration_seconds == pytest.approx(250.753)
    # 标题为空时用 desc 首行并去掉话题标记
    assert media.title == "今天出摊啦"
    assert media.uploader == "牛老师"
    assert media.thumbnail == "https://sns-img.example/cover.jpg"


def test_parse_note_falls_back_to_first_topic_when_desc_is_only_topics() -> None:
    """desc 全是话题标签时，用第一个话题名当标题。"""

    state = make_state(
        {
            "noteId": "abc",
            "type": "video",
            "title": "",
            "desc": "#摆摊[话题]# #摆摊日记[话题]# #蚝蛋烧[话题]#",
            "video": {
                "media": {
                    "stream": {"h264": [{"masterUrl": "https://cdn.example/v.mp4", "size": 5_000}]}
                }
            },
        }
    )

    assert xiaohongshu.parse_note(state).title == "摆摊"


def test_parse_note_rejects_image_note() -> None:
    state = make_state({"noteId": "abc", "type": "normal", "title": "图文笔记"})

    with pytest.raises(xiaohongshu.XiaohongshuError, match="图文笔记"):
        xiaohongshu.parse_note(state)


def test_parse_note_requires_stream() -> None:
    state = make_state({"noteId": "abc", "type": "video", "title": "没 stream 的视频"})

    with pytest.raises(xiaohongshu.XiaohongshuError, match="取到视频"):
        xiaohongshu.parse_note(state)


def test_parse_note_rejects_empty_state() -> None:
    with pytest.raises(xiaohongshu.XiaohongshuError, match="读到笔记内容"):
        xiaohongshu.parse_note({})


def test_parse_note_reads_share_page_user_field() -> None:
    """分享页里作者字段是 nickName（大写 N），不能只认网页端的 nickname。"""

    state = make_state(
        {
            "noteId": "abc",
            "type": "video",
            "title": "标题",
            "user": {"nickName": "牛老师", "userId": "6007dac90000000001006b4e"},
            "video": {"media": {"stream": {"h264": [{"masterUrl": "https://cdn.example/v.mp4", "size": 1}]}}},
        }
    )

    assert xiaohongshu.parse_note(state).uploader == "牛老师"


def test_parse_note_truncates_long_description() -> None:
    """作品介绍落库前截断，与下载器链路的处理保持一致。"""

    state = make_state(
        {
            "noteId": "abc",
            "type": "video",
            "title": "标题",
            "desc": "很长的正文" * 1000,
            "video": {"media": {"stream": {"h264": [{"masterUrl": "https://cdn.example/v.mp4", "size": 1}]}}},
        }
    )

    description = xiaohongshu.parse_note(state).description

    assert description is not None
    assert len(description) == xiaohongshu.MAX_DESCRIPTION_CHARS


def test_load_state_keeps_undefined_word_in_text() -> None:
    """正文里出现 undefined 这个词时不该被悄悄替换成 null。"""

    html = (
        "<script>window.__INITIAL_STATE__="
        '{"noteData":{"data":{"noteData":{"noteId":"abc","desc":"undefined 是保留字"}}}}'
        "</script>"
    )

    state = xiaohongshu._load_state(html)

    assert state["noteData"]["data"]["noteData"]["desc"] == "undefined 是保留字"


def test_load_state_tolerates_bare_undefined() -> None:
    """页面里确实写了非法的 undefined 时，替换成 null 后仍要能解析。"""

    html = '<script>window.__INITIAL_STATE__={"noteData":{"data":{"noteData":undefined}}}</script>'

    assert xiaohongshu._load_state(html) == {"noteData": {"data": {"noteData": None}}}


def test_request_headers_include_configured_cookies(tmp_path: Path) -> None:
    """自建请求要能用上用户配置的凭据，登录态才会作用于解析这一步。"""

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".xiaohongshu.com\tTRUE\t/\tTRUE\t1893456000\ta1\tabc123\n"
        "www.douyin.com\tFALSE\t/\tFALSE\t1893456000\tttwid\tother\n",
        encoding="utf-8",
    )

    assert xiaohongshu.request_headers(cookie_file)["Cookie"] == "a1=abc123"
    assert "Cookie" not in xiaohongshu.request_headers(None)


def test_resolve_media_flags_login_page(monkeypatch) -> None:
    """被重定向到登录页时给出明确提示，而不是把登录页当笔记页去解析。"""

    monkeypatch.setattr(
        xiaohongshu,
        "fetch_share_page",
        lambda url, cookies_file=None: (
            "<html></html>",
            "https://www.xiaohongshu.com/login?redirectPath=%2Fexplore%2Fabc",
        ),
    )

    with pytest.raises(xiaohongshu.XiaohongshuError, match="登录"):
        xiaohongshu.resolve_media("https://www.xiaohongshu.com/explore/abc")


def test_resolve_with_fallback_retries_as_guest(monkeypatch) -> None:
    """带凭据被判风控时退回纯游客再试一次，而不是直接动用本机浏览器。"""

    seen: list[object] = []
    media = xiaohongshu.XiaohongshuMedia(
        title="游客可看",
        uploader=None,
        duration_seconds=6.0,
        thumbnail=None,
        description=None,
        candidates=["https://cdn.example/v.mp4"],
    )

    def fake_resolve(url: str, cookies_file: Path | None = None) -> xiaohongshu.XiaohongshuMedia:
        seen.append(cookies_file)
        if cookies_file is not None:
            raise xiaohongshu.XiaohongshuError("触发了小红书的风控拦截")
        return media

    monkeypatch.setattr(xiaohongshu, "resolve_media", fake_resolve)
    monkeypatch.setattr(
        xiaohongshu,
        "fetch_note_via_browser",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("游客请求成功就不该启动浏览器")),
    )
    cookie_file = Path("cookies.txt")

    assert xiaohongshu.resolve_with_fallback("https://xhslink.cn/o/abc", cookie_file) is media
    assert seen == [cookie_file, None]


def test_download_media_reuses_resolved_media(monkeypatch, tmp_path: Path) -> None:
    """传入已解析的结果时不再请求分享页，直接下直链。"""

    downloaded: list[str] = []
    monkeypatch.setattr(
        xiaohongshu,
        "resolve_with_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不该重新解析")),
    )

    def fake_download(url: str, target: Path) -> None:
        downloaded.append(url)
        target.write_bytes(b"mp4")

    monkeypatch.setattr(xiaohongshu, "_download", fake_download)
    media = xiaohongshu.XiaohongshuMedia(
        title="标题",
        uploader=None,
        duration_seconds=None,
        thumbnail=None,
        description=None,
        candidates=["https://cdn.example/v.mp4"],
    )

    target = xiaohongshu.download_media("https://xhslink.cn/o/abc", tmp_path, media)

    assert target.read_bytes() == b"mp4"
    assert downloaded == ["https://cdn.example/v.mp4"]


def test_download_media_rejects_html_error_page(monkeypatch, tmp_path: Path) -> None:
    """直链返回 HTML 错误页时要换下一个候选，而不是把网页存成 mp4 交给 FFmpeg。"""

    class FakeResponse:
        def __init__(self) -> None:
            self.headers = {"content-type": "text/html; charset=utf-8"}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, _size: int):
            return iter([b"<html>403</html>"])

    monkeypatch.setattr(
        xiaohongshu.httpx, "stream", lambda *_args, **_kwargs: nullcontext(FakeResponse())
    )
    media = xiaohongshu.XiaohongshuMedia(
        title="标题",
        uploader=None,
        duration_seconds=None,
        thumbnail=None,
        description=None,
        candidates=["https://cdn.example/bad.mp4"],
    )

    with pytest.raises(xiaohongshu.XiaohongshuError, match="下载小红书视频失败"):
        xiaohongshu.download_media("https://xhslink.cn/o/abc", tmp_path, media)
