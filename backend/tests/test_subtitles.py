from app.infrastructure.subtitles import parse_subtitle, parse_timestamp


def test_parse_timestamp_supports_srt_and_vtt() -> None:
    assert parse_timestamp("00:01:02,345") == 62_345
    assert parse_timestamp("01:02.500") == 62_500


def test_parse_subtitle_keeps_timestamps_and_removes_duplicate(tmp_path) -> None:
    subtitle = tmp_path / "sample.srt"
    subtitle.write_text(
        """1
00:00:00,000 --> 00:00:02,000
<b>第一句</b>

2
00:00:02,000 --> 00:00:04,000
第一句

3
00:00:04,000 --> 00:00:06,500
第二句
""",
        encoding="utf-8",
    )

    chunks = parse_subtitle(subtitle)

    assert [chunk.text for chunk in chunks] == ["第一句", "第二句"]
    assert chunks[0].start_ms == 0
    assert chunks[1].end_ms == 6_500

