from app.application.exporters import export_document, timestamp
from app.application.text_formatting import join_paragraph, transcript_as_text
from app.infrastructure.models import Document, TranscriptSegment


def sample_document() -> Document:
    document = Document(
        id="document-id",
        title="测试文案",
        platform="local",
        source_type="file",
        source_value="sample.wav",
        status="draft",
    )
    document.segments = [
        TranscriptSegment(position=0, start_ms=0, end_ms=1500, raw_text="第一句", text="第一句"),
        TranscriptSegment(position=1, start_ms=1500, end_ms=3000, raw_text="第二句", text="修改后的第二句"),
    ]
    return document


def test_timestamp() -> None:
    assert timestamp(3_723_456) == "01:02:03,456"


def test_export_formats_use_edited_text() -> None:
    document = sample_document()
    text, _, extension = export_document(document, "txt")
    srt, _, _ = export_document(document, "srt")
    vtt, _, _ = export_document(document, "vtt")
    json_text, _, _ = export_document(document, "json")

    assert extension == "txt"
    # 一条分段一行，不额外补标点（补标点会让导出的文字与编辑页里看到的不一致）
    assert text == "第一句\n修改后的第二句"
    assert "00:00:01,500 --> 00:00:03,000" in srt
    assert vtt.startswith("WEBVTT")
    assert '"raw_text": "第二句"' in json_text


def test_txt_export_uses_paragraph_body_for_blank_lines() -> None:
    """正文一条分段一行；整理过（paragraphs=True）段落之间空一行。"""

    document = sample_document()
    document.segments[0].text = "其实面对折叠手机"
    document.segments[1].text = "我们始终会有个绕不过的问题"

    line_by_line, _, _ = export_document(document, "txt")
    paragraphs, _, _ = export_document(document, "txt", paragraphs=True)

    assert line_by_line == "其实面对折叠手机\n我们始终会有个绕不过的问题"
    assert paragraphs == "其实面对折叠手机\n\n我们始终会有个绕不过的问题"


def test_paragraph_joining_rules() -> None:
    """中文片段直接相接；英文/数字相邻才补空格。"""

    assert join_paragraph(["所有在职场里跟同学说", "你们自己认真想一件事儿"]) == (
        "所有在职场里跟同学说你们自己认真想一件事儿"
    )
    assert join_paragraph(["hello", "world"]) == "hello world"
    assert join_paragraph(["第 1 名", "第 2 名"]) == "第 1 名第 2 名"

    assert transcript_as_text([(0, 1000, "已经有句号。")]) == "已经有句号。"
    assert transcript_as_text([(0, 1000, "没有标点")]) == "没有标点"

