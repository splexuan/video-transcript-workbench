"""平台字幕解析：不依赖网络，只验证纯函数。"""

from app.infrastructure.bilibili import _request_headers, parse_platform_subtitles, pick_subtitle


def test_parse_platform_subtitles_maps_seconds_to_milliseconds() -> None:
    payload = {
        "body": [
            {"from": 0, "to": 2.5, "content": "第一句"},
            {"from": 2.5, "to": 5, "content": "第二句\n接着"},
            {"from": 5, "to": 6, "content": "   "},
        ]
    }

    chunks = parse_platform_subtitles(payload)

    assert [chunk.text for chunk in chunks] == ["第一句", "第二句 接着"]
    assert chunks[0].start_ms == 0
    assert chunks[0].end_ms == 2500
    assert chunks[1].start_ms == 2500


def test_parse_platform_subtitles_handles_empty_payload() -> None:
    assert parse_platform_subtitles({}) == []
    assert parse_platform_subtitles({"body": []}) == []


def test_pick_subtitle_prefers_ai_chinese_then_any() -> None:
    subtitles = [
        {"lan": "en", "subtitle_url": "https://example.com/en.json"},
        {"lan": "zh-CN", "subtitle_url": "https://example.com/zh.json"},
        {"lan": "ai-zh", "subtitle_url": "https://example.com/ai.json"},
    ]

    assert pick_subtitle(subtitles)["lan"] == "ai-zh"
    assert pick_subtitle([{"lan": "zh-CN", "subtitle_url": "u"}])["lan"] == "zh-CN"
    assert pick_subtitle([{"lan": "fr", "subtitle_url": "u"}])["lan"] == "fr"
    # 没有可用地址时视为没有字幕
    assert pick_subtitle([{"lan": "ai-zh", "subtitle_url": ""}]) is None
    assert pick_subtitle([]) is None


def test_request_headers_add_cookie_only_when_provided(tmp_path) -> None:
    """配了访问凭据，B站字幕接口才带上 Cookie；只带本平台的字段。"""

    assert "Cookie" not in _request_headers(None)

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".bilibili.com\tTRUE\t/\tTRUE\t1893456000\tSESSDATA\tabc\n"
        ".douyin.com\tTRUE\t/\tTRUE\t1893456000\tttwid\txyz\n",
        encoding="utf-8",
    )

    headers = _request_headers(cookie_file)

    assert headers["Cookie"] == "SESSDATA=abc"
    assert headers["Referer"] == "https://www.bilibili.com"
