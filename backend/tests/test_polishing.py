"""智能分句整理逻辑的测试：滚动重复合并、补标点与自然段。"""

from __future__ import annotations

from app.application.polishing import (
    collapse_punctuation,
    looks_punctuated,
    merge_overlapping_lines,
    polish_lines,
)
from app.domain import JobStage, JobStatus
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Document, Job, TranscriptSegment


def test_merge_overlapping_lines_handles_rolling_subtitles() -> None:
    # 累积式：后一条包含前一条 → 用新的替换旧的
    merged, dropped = merge_overlapping_lines(["今天", "今天天气", "今天天气不错"])
    assert merged == ["今天天气不错"]
    assert dropped == 2

    # 尾缀重复（滚动残影）→ 丢弃
    merged, dropped = merge_overlapping_lines(["我们走吧", "走吧"])
    assert merged == ["我们走吧"]
    assert dropped == 1

    # 结尾与下一条开头重叠 → 去掉重复的部分再接上
    merged, dropped = merge_overlapping_lines(["这期聊导出设置", "导出设置和码率"])
    assert merged == ["这期聊导出设置和码率"]
    assert dropped == 1

    # 判断不了重叠就原样保留：宁可留下重复，也不猜着删内容
    merged, dropped = merge_overlapping_lines(["大家好", "今天聊聊"])
    assert merged == ["大家好", "今天聊聊"]
    assert dropped == 0

    # 空条目直接跳过
    merged, dropped = merge_overlapping_lines(["  ", "有内容"])
    assert merged == ["有内容"]
    assert dropped == 0


def test_polish_lines_subtitle_merges_then_punctuates() -> None:
    lines = ["今天天气很", "天气很好", "公园里有", "很多人"]
    calls: list[str] = []

    def fake_punctuate(text: str) -> str:
        calls.append(text)
        return "今天天气很好。公园里有很多人。"

    result = polish_lines(lines, subtitle=True, punctuate=fake_punctuate)

    # 合并重复后才送给标点模型，标点不会加在重复内容上
    assert calls == ["今天天气很好公园里有很多人"]
    assert result.punctuated is True
    assert result.text == "今天天气很好。公园里有很多人。"
    assert result.sentences == 2
    assert result.merged == 1


def test_polish_lines_asr_regroups_long_segment_into_paragraphs() -> None:
    """识别结果自带标点：不补标点，只把长段切成句子再按句数成段。"""

    lines = ["第一句话。第二句话。第三句话。第四句话。第五句话。第六句话。第七句话。"]

    result = polish_lines(lines, subtitle=False)

    assert result.punctuated is False
    assert result.sentences == 7
    paragraphs = result.text.split("\n\n")
    assert len(paragraphs) == 2
    assert paragraphs[0].count("。") == 5
    assert paragraphs[1].count("。") == 2


def test_polish_lines_without_punctuate_keeps_text() -> None:
    """没有标点模型时（punctuate 为 None）不补标点、不合并，只做切句成段。"""

    result = polish_lines(["没有标点的字幕文本"], subtitle=True, punctuate=None)

    assert result.punctuated is False
    assert result.merged == 0
    assert result.text == "没有标点的字幕文本"


def test_collapse_punctuation_folds_repeats() -> None:
    assert collapse_punctuation("谢谢。。大家") == "谢谢。大家"
    assert collapse_punctuation("你好？？？") == "你好？"
    assert collapse_punctuation("真的。，然后") == "真的。然后"
    assert collapse_punctuation("，，先说这个") == "先说这个"
    # 正常的单个标点与引号不动
    assert collapse_punctuation('他说："开始吧！"') == '他说："开始吧！"'


def test_polish_lines_skips_model_when_body_already_punctuated() -> None:
    """已经整理过的正文不该再送标点模型：否则标点会叠成「。。。」。"""

    calls: list[str] = []

    def fake_punctuate(text: str) -> str:
        calls.append(text)
        return "不该被调用"

    lines = ["谢谢大家。。。", "。。现在找个工作还挺难的。。。", "我很幸运。"]
    result = polish_lines(lines, subtitle=True, punctuate=fake_punctuate)

    assert calls == []
    assert result.punctuated is False
    # 重复标点被折叠，段首的孤立标点被丢掉
    assert result.text == "谢谢大家。现在找个工作还挺难的。我很幸运。"

    # 判定本身：有标点的正文才算已整理过
    assert looks_punctuated("谢谢大家。现在找个工作还挺难的。我们接着往下说。") is True
    assert looks_punctuated("谢谢大家现在找个工作还挺难的我们接着往下说下去吧") is False


def _seed_document(transcript_source: str) -> str:
    """造一条文案：字幕来源需要标点模型，识别来源自带标点。"""

    init_database()
    with SessionLocal() as session:
        document = Document(
            title="整理测试",
            platform="bilibili",
            source_type="url",
            source_value="https://www.bilibili.com/video/BV1xx411c7mD",
            status="draft",
        )
        session.add(document)
        session.flush()
        session.add(
            Job(
                document_id=document.id,
                platform="bilibili",
                source_type="url",
                source_value="https://www.bilibili.com/video/BV1xx411c7mD",
                mode="fast",
                status=JobStatus.COMPLETED.value,
                stage=JobStage.DONE.value,
                progress=100,
                message="完成",
                transcript_source=transcript_source,
            )
        )
        document.segments.extend(
            [
                TranscriptSegment(
                    position=0, start_ms=0, end_ms=1_000, raw_text="今天天气很", text="今天天气很"
                ),
                TranscriptSegment(
                    position=1, start_ms=1_000, end_ms=2_000, raw_text="天气很好", text="天气很好"
                ),
            ]
        )
        session.commit()
        return document.id


def test_polish_asks_for_punctuation_model_when_missing() -> None:
    """字幕文案没有标点模型时，接口给一条可读的提示，而不是 500。"""

    from fastapi.testclient import TestClient

    from app.main import app

    # 测试数据目录是临时目录，本来就没有安装标点模型
    document_id = _seed_document("subtitle")
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/polish")

    assert response.status_code == 400
    assert "标点" in response.json()["detail"]


def test_polish_asr_needs_no_punctuation_model() -> None:
    """识别结果自带标点：不装标点模型也能整理成自然段。"""

    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document("asr")
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/polish")

    assert response.status_code == 200
    payload = response.json()
    assert payload["punctuated"] is False
    assert payload["text"]
    assert payload["paragraphs"] >= 1


def test_apply_polish_stores_paragraphs_as_segments() -> None:
    """整理结果落库后：分段就是自然段，正文与时间轴同形态，重新打开不会变回一行一句。"""

    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document("subtitle")
    with TestClient(app) as client:
        response = client.post(
            f"/api/documents/{document_id}/polish/apply",
            json={"text": "今天天气很好。\n\n公园里有很多人。"},
        )

        assert response.status_code == 200
        detail = response.json()
        assert [item["text"] for item in detail["segments"]] == [
            "今天天气很好。",
            "公园里有很多人。",
        ]
        # 段落时间按字数比例分摊到原来的时间范围，识别原文按重叠保留
        assert detail["segments"][0]["start_ms"] == 0
        assert detail["segments"][-1]["end_ms"] == 2_000
        assert detail["segments"][0]["raw_text"]
        # 正文形态随之变成自然段（段落之间空行）
        assert detail["paragraph_body"] is True

        # 正文（txt / json）与分段一致：每段一条，段落之间空行
        txt = client.get(f"/api/documents/{document_id}/export?format=txt").content
        assert txt.decode("utf-8-sig") == "今天天气很好。\n\n公园里有很多人。"
        payload = client.get(f"/api/documents/{document_id}/export?format=json").json()
        assert payload["paragraphs"] == ["今天天气很好。", "公园里有很多人。"]

        # 按分段逐条保存（正文里没有空行）→ 回到「一行一句」形态
        segments = detail["segments"]
        plain = client.put(
            f"/api/documents/{document_id}/segments",
            json={
                "segments": [
                    {
                        "id": item["id"],
                        "position": item["position"],
                        "start_ms": item["start_ms"],
                        "end_ms": item["end_ms"],
                        "text": item["text"],
                    }
                    for item in segments
                ]
            },
        )
        assert plain.status_code == 200
        assert client.get(f"/api/documents/{document_id}").json()["paragraph_body"] is False

        # 没有内容的正文给一条可读错误
        empty = client.post(f"/api/documents/{document_id}/polish/apply", json={"text": "   "})
        assert empty.status_code == 400


def test_restore_lines_rebuilds_subtitle_lines() -> None:
    """整理过的文案能从识别原文退回「一行一句」，把结构改动撤掉。"""

    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document("subtitle")
    with TestClient(app) as client:
        applied = client.post(
            f"/api/documents/{document_id}/polish/apply",
            json={"text": "今天天气很好。\n\n公园里有很多人。"},
        ).json()
        assert applied["paragraph_body"] is True
        assert len(applied["segments"]) == 2

        restored = client.post(f"/api/documents/{document_id}/restore-lines")
        assert restored.status_code == 200
        detail = restored.json()

        assert detail["paragraph_body"] is False
        # 识别原文按顺序、去重后逐行重建
        assert [item["text"] for item in detail["segments"]] == ["今天天气很", "天气很好"]
        txt = client.get(f"/api/documents/{document_id}/export?format=txt").content
        assert txt.decode("utf-8-sig") == "今天天气很\n天气很好"
