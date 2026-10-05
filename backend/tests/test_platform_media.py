"""平台错误归类、链接归一化与任务提示：不依赖网络，只验证纯函数。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.worker import audio_stage_message
from app.infrastructure import platform_media, xiaohongshu
from app.infrastructure.platform_media import (
    CookieInvalidError,
    CookieRequiredError,
    PlatformError,
    UnsupportedUrlError,
    classify_error,
    normalize_url,
    pick_title,
)


def test_classify_error_maps_missing_cookie() -> None:
    """抖音未配置 Cookie 时的原始报错要转成可引导用户的提示。"""

    error = classify_error(
        "ERROR: [Douyin] 6961737553342991651: Fresh cookies (not necessarily logged in) are needed",
        "douyin",
    )

    assert isinstance(error, CookieRequiredError)
    assert error.code == "COOKIE_REQUIRED"
    assert "抖音" in str(error)
    assert "一键获取访问权限" in str(error)


def test_classify_error_maps_expired_login() -> None:
    error = classify_error("ERROR: Sign in to confirm you're not a bot", "xiaohongshu")

    assert isinstance(error, CookieInvalidError)
    assert error.code == "COOKIE_INVALID"
    assert "重新获取" in str(error)


def test_classify_error_maps_unsupported_link() -> None:
    error = classify_error("ERROR: Unsupported URL: https://example.com/post/1", "douyin")

    assert isinstance(error, UnsupportedUrlError)
    assert error.code == "UNSUPPORTED_SOURCE"


def test_classify_error_falls_back_to_generic_platform_error() -> None:
    error = classify_error("ERROR: HTTP Error 500", "bilibili")

    assert type(error) is PlatformError
    assert error.code == "PLATFORM_ERROR"
    assert "B站" in str(error)


def test_audio_stage_message_distinguishes_reasons() -> None:
    skipped = SimpleNamespace(platform="bilibili", prefer_subtitle=False)
    no_subtitle = SimpleNamespace(platform="bilibili", prefer_subtitle=True)
    plain_platform = SimpleNamespace(platform="douyin", prefer_subtitle=True)

    assert audio_stage_message(skipped, "B站") == "已跳过平台字幕，正在下载音轨"
    assert audio_stage_message(no_subtitle, "B站") == "没有可用字幕，正在下载音轨"
    assert "抖音没有可读取的字幕" in audio_stage_message(plain_platform, "抖音")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "https://www.douyin.com/jingxuan?modal_id=7000000000000000000",
            "https://www.douyin.com/video/7000000000000000000",
        ),
        (
            "https://www.douyin.com/user/MS4wLjABAAAA?modal_id=1234567890123456789&x=1",
            "https://www.douyin.com/video/1234567890123456789",
        ),
        (
            "https://www.douyin.com/video/7000000000000000000",
            "https://www.douyin.com/video/7000000000000000000",
        ),
        (
            "https://www.iesdouyin.com/share/video/7000000000000000000/?region=CN",
            "https://www.douyin.com/video/7000000000000000000",
        ),
        # 分享短链会发起网络跳转，放到单独用例里用 monkeypatch 覆盖
    ],
)
def test_normalize_douyin_urls(source: str, expected: str) -> None:
    """精选、发现、搜索、作者主页里复制的 modal_id 链接要归一成作品地址。"""

    assert normalize_url(source, "douyin") == expected


def test_normalize_douyin_short_link_uses_redirect(monkeypatch) -> None:
    """分享短链先本地跳转拿到作品地址，去掉跳转带来的追踪参数。"""

    monkeypatch.setattr(
        platform_media,
        "_follow_redirect",
        lambda url: "https://www.douyin.com/video/7679366929526665627?previous_page=app_code_link",
    )

    assert normalize_url("https://v.douyin.com/mbD3tCXlUZg", "douyin") == (
        "https://www.douyin.com/video/7679366929526665627"
    )


def test_normalize_douyin_short_link_falls_back_when_redirect_fails(monkeypatch) -> None:
    """跳转失败时保留原短链，让下载器自己再试一次。"""

    monkeypatch.setattr(platform_media, "_follow_redirect", lambda url: None)
    url = "https://v.douyin.com/mbD3tCXlUZg"

    assert normalize_url(url, "douyin") == url


def test_normalize_rejects_douyin_image_post() -> None:
    """图文作品没有音频，要给出可理解的提示而不是交给下载器乱试。"""

    with pytest.raises(UnsupportedUrlError):
        normalize_url("https://www.douyin.com/note/7000000000000000000", "douyin")


def test_normalize_leaves_other_platforms_untouched() -> None:
    url = "https://www.bilibili.com/video/BV1test"

    assert normalize_url(url, "bilibili") == url


def test_normalize_xiaohongshu_short_link_uses_redirect(monkeypatch) -> None:
    """分享短链要先跳转：最终地址带 xsec_token 才能拿到笔记内容。"""

    monkeypatch.setattr(
        platform_media,
        "_follow_redirect",
        lambda url, headers=None: "https://www.xiaohongshu.com/explore/674051740000000007027a15?xsec_token=abc",
    )

    assert normalize_url("http://xhslink.com/a/JdMVQvX9NSab", "xiaohongshu") == (
        "https://www.xiaohongshu.com/explore/674051740000000007027a15?xsec_token=abc"
    )


def test_normalize_xiaohongshu_mobile_short_link(monkeypatch) -> None:
    """手机分享短链 xhslink.cn 同样要先跳转。"""

    monkeypatch.setattr(
        platform_media,
        "_follow_redirect",
        lambda url, headers=None: (
            "https://www.xiaohongshu.com/discovery/item/6f0000000000000000000001"
            "?xsec_token=abc&xsec_source=pc_share"
        ),
    )

    assert normalize_url("https://xhslink.cn/o/1ftpFBsi64Z", "xiaohongshu") == (
        "https://www.xiaohongshu.com/discovery/item/6f0000000000000000000001"
        "?xsec_token=abc&xsec_source=pc_share"
    )


def test_normalize_xiaohongshu_keeps_short_link_when_redirect_fails(monkeypatch) -> None:
    """跳转失败时保留原链接，让下载器自己再试一次。"""

    monkeypatch.setattr(platform_media, "_follow_redirect", lambda url, headers=None: None)
    url = "http://xhslink.com/a/JdMVQvX9NSab"

    assert normalize_url(url, "xiaohongshu") == url


def test_normalize_xiaohongshu_keeps_direct_note_links() -> None:
    explore = "https://www.xiaohongshu.com/explore/674051740000000007027a15?xsec_token=abc"
    discovery = "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15"

    assert normalize_url(explore, "xiaohongshu") == explore
    assert normalize_url(discovery, "xiaohongshu") == discovery


def test_classify_error_maps_note_without_video() -> None:
    """取不到视频时要同时提示两种可能：图文笔记，或需要登录才能查看。"""

    error = classify_error("ERROR: No video formats found", "xiaohongshu")

    assert isinstance(error, UnsupportedUrlError)
    assert "图文" in str(error)
    assert "浏览器登录" in str(error)


def test_pick_title_keeps_real_title() -> None:
    assert pick_title({"title": "香妃蛋糕也太香了吧"}) == "香妃蛋糕也太香了吧"


def test_pick_title_falls_back_to_description_for_placeholder() -> None:
    """下载器只给占位标题时，用描述首行代替，并去掉话题标记。"""

    payload = {
        "title": "XiaoHongShu video #6a8c6316000000001402b19f",
        "description": "沉默了7年，一片悼文解开了所有真相  #董卿[话题]#\n第二行不取",
    }

    assert pick_title(payload) == "沉默了7年，一片悼文解开了所有真相"


def test_pick_title_handles_missing_values() -> None:
    assert pick_title({}) == "未命名视频"
    # 占位标题且没有描述时，保留下载器给的值，至少还能看出作品 id
    assert pick_title({"title": "Douyin video #7679366929526665627"}) == (
        "Douyin video #7679366929526665627"
    )


def test_resolution_is_reused_by_download(monkeypatch, tmp_path) -> None:
    """解析与下载只请求一次分享页：第二次解析被风控拦下会让整个任务白白失败。"""

    calls: list[str] = []
    media = xiaohongshu.XiaohongshuMedia(
        title="笔记",
        uploader=None,
        duration_seconds=8.0,
        thumbnail=None,
        description=None,
        candidates=["https://cdn.example/v.mp4"],
    )

    def fake_resolve(url: str, cookies_file=None) -> xiaohongshu.XiaohongshuMedia:
        calls.append(url)
        return media

    # 缓存跨用例共享，先清掉上一次的残留
    platform_media._resolve_cache.clear()
    monkeypatch.setattr(platform_media.xiaohongshu, "resolve_with_fallback", fake_resolve)
    monkeypatch.setattr(
        platform_media.xiaohongshu,
        "_download",
        lambda url, target: target.write_bytes(b"mp4"),
    )
    url = "https://www.xiaohongshu.com/explore/6f0000000000000000000001"

    info = platform_media.resolve_video(url, "xiaohongshu")
    target = platform_media.download_audio_source(url, tmp_path, "xiaohongshu")

    assert info.title == "笔记"
    assert info.duration_seconds == 8.0
    assert target.read_bytes() == b"mp4"
    assert calls == [url]


def test_run_ytdlp_spawns_subprocess_when_not_frozen(monkeypatch) -> None:
    """开发版起子进程，并把 Cookie 临时文件通过 `--cookies` 传下去。"""

    seen: dict[str, list[str]] = {}

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(platform_media.subprocess, "run", fake_run)

    result = platform_media.run_ytdlp(
        ["--dump-single-json", "https://example.com/v"],
        cookies_file=Path("cookies.txt"),
    )

    assert seen["command"] == [
        sys.executable,
        "-m",
        "yt_dlp",
        "--cookies",
        "cookies.txt",
        "--dump-single-json",
        "https://example.com/v",
    ]
    assert result.returncode == 0


def test_run_ytdlp_runs_in_process_when_frozen(monkeypatch) -> None:
    """打包版没有独立解释器，改为同进程调用；这条真的跑一次 `--version`。

    不联网，但能验证三件事：argv 不带程序名（带了会被当作品地址去下载）、
    退出码换算正确、输出被收进缓冲区。
    """

    monkeypatch.setattr(sys, "frozen", True, raising=False)

    result = platform_media.run_ytdlp(["--version"])

    assert result.returncode == 0
    assert result.stdout.strip()


def test_frozen_ytdlp_gets_bundled_ffmpeg_location(monkeypatch) -> None:
    """打包版要把自带 FFmpeg 的位置告诉 yt-dlp。

    yt-dlp 自己只从 PATH 找 ffmpeg，而打包版的 ffmpeg.exe 在资源目录里；
    不显式指路的话，需要合并音视频或转换格式时会报「ffmpeg is not installed」。
    """

    captured: list[list[str]] = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(platform_media, "find_tool", lambda _name: r"C:\app\_internal\ffmpeg.exe")
    monkeypatch.setattr(platform_media, "_ytdlp_entry", lambda: captured.append)

    result = platform_media.run_ytdlp(["--dump-single-json", "https://example.com/v"])

    assert result.returncode == 0
    assert captured == [
        ["--ffmpeg-location", r"C:\app\_internal\ffmpeg.exe", "--dump-single-json", "https://example.com/v"]
    ]


def test_in_process_ytdlp_keeps_error_text(monkeypatch) -> None:
    """参数出错时要有非零退出码与 stderr，否则 `_last_error` 给不出可读原因。"""

    monkeypatch.setattr(sys, "frozen", True, raising=False)

    result = platform_media.run_ytdlp(["--this-option-does-not-exist"])

    assert result.returncode != 0
    assert result.stderr.strip()


def test_audio_download_takes_the_smallest_stream(monkeypatch, tmp_path) -> None:
    """下载音轨要按体积从小到大取：识别只需要声音，拉整段高清视频是白下载。

    实测抖音 16 分钟视频 51.5 MB → 27.9 MB、B站 10.0 MB → 4.2 MB。
    """

    seen: dict[str, list[str]] = {}

    def fake_run(args, timeout=300, cookies_file=None) -> subprocess.CompletedProcess[str]:
        seen["args"] = list(args)
        (tmp_path / "source.m4a").write_bytes(b"audio")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(platform_media, "run_ytdlp", fake_run)

    target = platform_media.download_audio_source(
        "https://www.bilibili.com/video/BV1test", tmp_path, "bilibili"
    )

    assert target.read_bytes() == b"audio"
    # 纯音频优先；只写 -S +size 会让 B站 退化成音视频合流
    assert seen["args"][:2] == ["-f", "bestaudio/best"]
    assert "+size" in seen["args"]


def test_resolution_cache_expires(monkeypatch, tmp_path) -> None:
    """超过复用窗口后必须重新解析，不能把过期直链当现成的用。"""

    calls: list[str] = []
    media = xiaohongshu.XiaohongshuMedia(
        title="笔记",
        uploader=None,
        duration_seconds=None,
        thumbnail=None,
        description=None,
        candidates=["https://cdn.example/v.mp4"],
    )

    def fake_resolve(url: str, cookies_file=None) -> xiaohongshu.XiaohongshuMedia:
        calls.append(url)
        return media

    platform_media._resolve_cache.clear()
    # 窗口设成负数：等价于「上次解析早就过期了」
    monkeypatch.setattr(platform_media, "RESOLVE_REUSE_SECONDS", -1.0)
    monkeypatch.setattr(platform_media.xiaohongshu, "resolve_with_fallback", fake_resolve)
    monkeypatch.setattr(
        platform_media.xiaohongshu,
        "_download",
        lambda url, target: target.write_bytes(b"mp4"),
    )
    url = "https://www.xiaohongshu.com/explore/expired"

    platform_media.resolve_video(url, "xiaohongshu")
    platform_media.download_audio_source(url, tmp_path, "xiaohongshu")

    assert calls == [url, url]
