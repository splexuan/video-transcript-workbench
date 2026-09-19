"""自动切句、时间轴细分与段落分组的测试。"""

from __future__ import annotations

from app.application.text_formatting import (
    group_paragraphs,
    lines_as_text,
    paragraphs_as_text,
    reflow_segments,
    split_sentences,
)
from app.domain import JobStage, JobStatus
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Document, Job, TranscriptSegment


def test_split_sentences_chinese_and_english() -> None:
    text = "大家好。这一期聊导出设置！码率越高画面越清晰,但文件也越大。对吗？"
    sentences = split_sentences(text)
    assert len(sentences) == 4
    assert sentences[0] == "大家好。"
    assert sentences[-1] == "对吗？"


def test_split_sentences_keeps_decimals_and_closers() -> None:
    text = '增长了3.5倍。他说："开始吧！"然后结束了。'
    sentences = split_sentences(text)
    # 小数点不切句；引号闭合并入前一句
    assert any("3.5倍" in item for item in sentences)
    assert any(item.endswith('！"') for item in sentences)


def test_split_sentences_fallback_for_unpunctuated_blob() -> None:
    text = "这是一段没有任何标点的超长文本" * 20
    sentences = split_sentences(text)
    assert len(sentences) > 1
    assert all(len(item) >= 30 for item in sentences)


def test_lines_as_text_keeps_one_segment_per_line() -> None:
    """字幕来源按原样一行一句：不补标点、不合并段落。

    实测 B站 AI 字幕整段没有标点、时间戳首尾相连（间隔恒为 0），条目边界也和
    语义无关，所以规则补标点只会产生「随机位置的逗号」，不如原样展示。
    """

    segments = [
        (0, 1_000, "其实面对折叠手机"),
        (1_000, 2_000, "我们始终会有个绕不过的问题"),
    ]

    assert lines_as_text(segments) == "其实面对折叠手机\n我们始终会有个绕不过的问题"
    # 空片段不占行
    assert lines_as_text([(0, 1_000, "   "), (1_000, 2_000, "有内容")]) == "有内容"


def test_reflow_segments_distributes_time_by_length() -> None:
    blobs = [(0, 30_000, "短句。这是一个长度大约是前面句子两倍的长句子。")]
    chunks = reflow_segments(blobs)
    assert len(chunks) == 2
    assert chunks[0].start_ms == 0
    assert chunks[0].end_ms < chunks[1].end_ms
    assert chunks[-1].end_ms == 30_000
    # 时间连续无缝隙
    assert chunks[1].start_ms == chunks[0].end_ms


def test_reflow_segments_without_time() -> None:
    chunks = reflow_segments([(None, None, "第一句。第二句。")])
    assert [item.text for item in chunks] == ["第一句。", "第二句。"]
    assert all(item.start_ms is None and item.end_ms is None for item in chunks)


def test_group_paragraphs_by_gap_and_limit() -> None:
    # 前两句连续（间隙 0），第三句与第二句间隔 2 秒 → 另起一段
    segments = [
        (0, 2_000, "第一句。"),
        (2_000, 4_000, "第二句。"),
        (6_000, 8_000, "第三句。"),
    ]
    paragraphs = group_paragraphs(segments)
    assert len(paragraphs) == 2
    assert paragraphs[0] == ["第一句。", "第二句。"]
    assert paragraphs[1] == ["第三句。"]


def test_group_paragraphs_sentence_cap() -> None:
    segments = [(index * 1_000, index * 1_000 + 500, f"第{index}句。") for index in range(10)]
    paragraphs = group_paragraphs(segments)
    assert all(len(block) <= 6 for block in paragraphs)
    assert sum(len(block) for block in paragraphs) == 10


def test_paragraphs_as_text_joins_within_paragraph() -> None:
    segments = [(0, 1_000, "大家好。"), (1_000, 2_000, "今天聊导出。"), (5_000, 6_000, "第二段开始。")]
    text = paragraphs_as_text(segments)
    assert text == "大家好。今天聊导出。\n\n第二段开始。"


def _seed_document() -> str:
    init_database()
    with SessionLocal() as session:
        document = Document(
            title="整理测试",
            platform="local",
            source_type="file",
            source_value="D:/素材/采访.mp4",
            status="draft",
            duration_seconds=60.0,
        )
        session.add(document)
        session.flush()
        session.add(
            Job(
                document_id=document.id,
                platform="local",
                source_type="file",
                source_value="D:/素材/采访.mp4",
                mode="fast",
                status=JobStatus.COMPLETED.value,
                stage=JobStage.DONE.value,
                progress=100,
                message="完成",
            )
        )
        document.segments.extend(
            [
                TranscriptSegment(
                    position=0,
                    start_ms=0,
                    end_ms=20_000,
                    raw_text="大家好，欢迎收看本期节目。今天我们聊一聊导出设置！首先说码率。",
                    text="大家好，欢迎收看本期节目。今天我们聊一聊导出设置！首先说码率。",
                ),
                TranscriptSegment(
                    position=1,
                    start_ms=20_000,
                    end_ms=30_000,
                    raw_text="然后是色彩空间。",
                    text="然后是色彩空间。",
                ),
            ]
        )
        session.commit()
        return document.id


def test_auto_format_endpoint_and_txt_export() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document()
    with TestClient(app) as client:
        detail = client.post(f"/api/documents/{document_id}/auto-format")
        assert detail.status_code == 200
        segments = detail.json()["segments"]
        # 20 秒的整段被切成 3 句，30 秒的切出 1 句
        assert len(segments) == 4
        assert segments[0]["text"] == "大家好，欢迎收看本期节目。"
        assert segments[-1]["text"] == "然后是色彩空间。"
        assert segments[0]["end_ms"] == segments[1]["start_ms"]
        assert "今天我们聊一聊导出设置" in segments[0]["raw_text"]
        assert detail.json()["word_count"] == sum(len(item["text"].strip()) for item in segments)

        empty = client.post("/api/documents/missing/auto-format")
        assert empty.status_code == 404

        exported = client.get(f"/api/documents/{document_id}/export?format=txt")
        assert exported.status_code == 200
        text = exported.content.decode("utf-8-sig")
        # 段内成句、段间空行：整段时间连续不会断段，应为单段
        assert "大家好，欢迎收看本期节目。今天我们聊一聊导出设置！首先说码率。然后是色彩空间。" in text

        payload = client.get(f"/api/documents/{document_id}/export?format=json").json()
        assert isinstance(payload["paragraphs"], list)
        assert payload["paragraphs"]


def test_edit_preserves_recognition_raw_text() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document()
    with TestClient(app) as client:
        original = client.get(f"/api/documents/{document_id}").json()
        original_raw = [item["raw_text"] for item in original["segments"]]
        edited_segments = [
            {
                "id": item["id"],
                "position": item["position"],
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "text": f"已校对：{item['text']}",
            }
            for item in original["segments"]
        ]

        response = client.put(
            f"/api/documents/{document_id}/segments",
            json={"segments": edited_segments},
        )

        assert response.status_code == 200
        assert [item["raw_text"] for item in response.json()["segments"]] == original_raw
        assert response.json()["segments"][0]["text"].startswith("已校对：")
