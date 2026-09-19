"""繁简统一：转写结果落库前把繁体转成简体。"""

from app.domain import TranscriptChunk
from app.infrastructure.text_variant import simplify_chunks, to_simplified

# 真实样本：faster-whisper small 对一段中文口语视频的输出（全繁体）
WHISPER_SAMPLE = "今天呢我想先來演一個場景,哇賽,你的新書包好好看,多少錢買的呀?"
EXPECTED = "今天呢我想先来演一个场景,哇赛,你的新书包好好看,多少钱买的呀?"


def test_to_simplified_converts_whisper_output() -> None:
    assert to_simplified(WHISPER_SAMPLE) == EXPECTED


def test_to_simplified_keeps_simplified_and_other_languages() -> None:
    """已经是简体、或非中文内容都不该被改动。"""

    assert to_simplified("今天天气不错，我们去公园吧。") == "今天天气不错，我们去公园吧。"
    assert to_simplified("Hello, this is a test.") == "Hello, this is a test."
    assert to_simplified("") == ""
    assert to_simplified("   ") == "   "


def test_simplify_chunks_keeps_timestamps() -> None:
    chunks = [
        TranscriptChunk(0, 2000, "這是第一句"),
        TranscriptChunk(2000, 4000, "這是第二句"),
    ]

    result = simplify_chunks(chunks)

    assert [item.text for item in result] == ["这是第一句", "这是第二句"]
    assert [item.start_ms for item in result] == [0, 2000]
    assert [item.end_ms for item in result] == [2000, 4000]
