"""智能分句整理逻辑的测试：滚动重复合并、补标点、自然段与一键落库。"""

from __future__ import annotations

from app.application.polishing import (
    collapse_punctuation,
    looks_punctuated,
    merge_overlapping_lines,
    polish_lines,
)
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Document, TranscriptSegment


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


def test_polish_lines_merges_then_punctuates() -> None:
    lines = ["今天天气很", "天气很好", "公园里有", "很多人"]
    calls: list[str] = []

    def fake_punctuate(text: str) -> str:
        calls.append(text)
        return "今天天气很好。公园里有很多人。"

    result = polish_lines(lines, punctuate=fake_punctuate)

    # 合并重复后才送给标点模型，标点不会加在重复内容上
    assert calls == ["今天天气很好公园里有很多人"]
    assert result.punctuated is True
    assert result.text == "今天天气很好。公园里有很多人。"
    assert result.sentences == 2
    assert result.merged == 1


def test_polish_lines_regroups_long_segment_into_paragraphs() -> None:
    """正文自带标点：不补标点（punctuate=None），只把长段切成句子再按句数成段。"""

    lines = ["第一句话。第二句话。第三句话。第四句话。第五句话。第六句话。第七句话。"]

    result = polish_lines(lines, punctuate=None)

    assert result.punctuated is False
    assert result.sentences == 7
    paragraphs = result.text.split("\n\n")
    assert len(paragraphs) == 2
    assert paragraphs[0].count("。") == 5
    assert paragraphs[1].count("。") == 2


def test_polish_lines_without_punctuate_keeps_text() -> None:
    """没有标点模型时（punctuate=None）不补标点，只做切句成段。"""

    result = polish_lines(["没有标点的文本"], punctuate=None)

    assert result.punctuated is False
    assert result.merged == 0
    assert result.text == "没有标点的文本"


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
    result = polish_lines(lines, punctuate=fake_punctuate)

    assert calls == []
    assert result.punctuated is False
    # 重复标点被折叠，段首的孤立标点被丢掉
    assert result.text == "谢谢大家。现在找个工作还挺难的。我很幸运。"

    # 判定本身：有标点的正文才算已整理过
    assert looks_punctuated("谢谢大家。现在找个工作还挺难的。我们接着往下说。") is True
    assert looks_punctuated("谢谢大家现在找个工作还挺难的我们接着往下说下去吧") is False


def _seed_document(texts: list[str]) -> str:
    """造一条带分段的文案；返回 id。"""

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
        document.segments.extend(
            TranscriptSegment(
                position=index,
                start_ms=index * 1_000,
                end_ms=index * 1_000 + 900,
                raw_text=text,
                text=text,
            )
            for index, text in enumerate(texts)
        )
        session.commit()
        return document.id


def test_polish_asks_for_punctuation_model_when_missing() -> None:
    """正文没有句末标点又没装标点模型时，接口给一条可读提示，而不是 500。"""

    from fastapi.testclient import TestClient

    from app.main import app

    # 测试数据目录是临时目录，本来就没有安装标点模型
    document_id = _seed_document(["今天天气很", "天气很好"])
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/polish")

    assert response.status_code == 400
    assert "标点" in response.json()["detail"]


def test_polish_applies_paragraphs_directly(monkeypatch) -> None:
    """点一下即生效：整理结果直接写回分段，返回体里的文案就是整理后的形态。"""

    from fastapi.testclient import TestClient

    from app.infrastructure import punctuation
    from app.main import app

    # 测试环境没有下载标点模型，用假的替掉真实推理
    monkeypatch.setattr(punctuation, "is_ready", lambda: True)
    monkeypatch.setattr(
        punctuation, "add_punctuation", lambda text: "今天天气很好。公园里有很多人。"
    )

    document_id = _seed_document(["今天天气很", "天气很好", "公园里有很多人"])
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/polish")
        reopened = client.get(f"/api/documents/{document_id}").json()

    assert response.status_code == 200
    payload = response.json()
    assert payload["punctuated"] is True
    # 返回体里带上整理后的文案：已经落库，重新打开就是同一份
    assert payload["document"]["paragraph_body"] is True
    assert payload["document"]["segments"] == reopened["segments"]
    # 两句话组成一个自然段（没到每段 5 句的上限）
    assert [item["text"] for item in reopened["segments"]] == ["今天天气很好。公园里有很多人。"]
    assert reopened["paragraph_body"] is True


def test_polish_regroups_punctuated_body_without_model() -> None:
    """正文自带标点：不装标点模型也能整理，并且直接落库。"""

    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document(
        ["第一句话。第二句话。第三句话。第四句话。第五句话。第六句话。第七句话。"]
    )
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/polish")
        reopened = client.get(f"/api/documents/{document_id}").json()

    assert response.status_code == 200
    payload = response.json()
    assert payload["punctuated"] is False
    assert payload["sentences"] == 7
    # 7 句按「每段最多 5 句」分成 2 段
    assert payload["paragraphs"] == 2
    assert len(reopened["segments"]) == 2
    assert reopened["paragraph_body"] is True


def test_saving_body_keeps_merged_line_intact() -> None:
    """整篇保存后「行就是分段」：把两行合并成一行，保存后不能再被拆开。"""

    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document(["把罐还给你了", "，给你。"])
    with TestClient(app) as client:
        saved = client.put(
            f"/api/documents/{document_id}/body",
            json={"text": "把罐还给你了，给你。"},
        )
        reopened = client.get(f"/api/documents/{document_id}").json()

    assert saved.status_code == 200
    body = saved.json()
    assert [item["text"] for item in body["segments"]] == ["把罐还给你了，给你。"]
    assert body["paragraph_body"] is False
    assert reopened["segments"] == body["segments"]


def test_saving_body_keeps_times_when_line_count_is_unchanged() -> None:
    """行数没变就地改字：分段 id 与时间码完全不动。"""

    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document(["第一句。", "第二句。"])
    with TestClient(app) as client:
        before = client.get(f"/api/documents/{document_id}").json()["segments"]
        saved = client.put(
            f"/api/documents/{document_id}/body",
            json={"text": "第一句改过了。\n第二句。"},
        ).json()

    assert [item["id"] for item in saved["segments"]] == [item["id"] for item in before]
    assert [(item["start_ms"], item["end_ms"]) for item in saved["segments"]] == [
        (item["start_ms"], item["end_ms"]) for item in before
    ]
    assert [item["text"] for item in saved["segments"]] == ["第一句改过了。", "第二句。"]


def test_saving_body_uses_blank_lines_for_paragraph_form() -> None:
    """草稿里有空行就是段落形态；空正文给一条可读错误。"""

    from fastapi.testclient import TestClient

    from app.main import app

    document_id = _seed_document(["第一段。", "第二段。"])
    with TestClient(app) as client:
        saved = client.put(
            f"/api/documents/{document_id}/body",
            json={"text": "第一段。\n\n第二段。"},
        )
        empty = client.put(f"/api/documents/{document_id}/body", json={"text": "   "})

    assert saved.status_code == 200
    assert saved.json()["paragraph_body"] is True
    assert [item["text"] for item in saved.json()["segments"]] == ["第一段。", "第二段。"]
    assert empty.status_code == 400


def test_saving_body_round_trips_the_draft() -> None:
    """所见即所得：保存后按正文规则重新拼装，必须与你写的一致。"""

    from fastapi.testclient import TestClient

    from app.application.text_formatting import transcript_as_text
    from app.main import app

    drafts = [
        "第一段。\n\n第二段。",
        "第一段。\n第二段。",
        "把两行并成一行。",
        "一\n\n二\n\n三",
    ]

    for draft in drafts:
        document_id = _seed_document(["旧内容一。", "旧内容二。"])
        with TestClient(app) as client:
            saved = client.put(
                f"/api/documents/{document_id}/body", json={"text": draft}
            ).json()

        rebuilt = transcript_as_text(
            [
                (item["start_ms"], item["end_ms"], item["text"])
                for item in saved["segments"]
            ],
            paragraphs=saved["paragraph_body"],
        )
        assert rebuilt == draft, f"{draft!r} 保存后变成了 {rebuilt!r}"


def test_saving_segments_keeps_paragraph_form(monkeypatch) -> None:
    """整理过的文案编辑保存后不该退回一行一句：正文形态只由智能分句/恢复改。"""

    from fastapi.testclient import TestClient

    from app.infrastructure import punctuation
    from app.main import app

    monkeypatch.setattr(punctuation, "is_ready", lambda: True)
    monkeypatch.setattr(
        punctuation, "add_punctuation", lambda text: "今天天气很好。公园里有很多人。"
    )

    document_id = _seed_document(["今天天气很", "天气很好", "公园里有很多人"])
    with TestClient(app) as client:
        applied = client.post(f"/api/documents/{document_id}/polish").json()
        segments = applied["document"]["segments"]
        saved = client.put(
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

    assert saved.status_code == 200
    assert saved.json()["paragraph_body"] is True


def test_restore_lines_rebuilds_original_segments(monkeypatch) -> None:
    """整理过之后能从识别原文退回「一行一句」。"""

    from fastapi.testclient import TestClient

    from app.infrastructure import punctuation
    from app.main import app

    monkeypatch.setattr(punctuation, "is_ready", lambda: True)
    monkeypatch.setattr(
        punctuation,
        "add_punctuation",
        lambda text: "今天天气很好。\n\n公园里有很多人。",
    )

    document_id = _seed_document(["今天天气很", "天气很好", "公园里有很多人"])
    with TestClient(app) as client:
        applied = client.post(f"/api/documents/{document_id}/polish").json()
        assert applied["document"]["paragraph_body"] is True

        restored = client.post(f"/api/documents/{document_id}/restore-lines")

    assert restored.status_code == 200
    detail = restored.json()
    assert detail["paragraph_body"] is False
    # 识别原文按顺序、去重后逐行重建
    assert [item["text"] for item in detail["segments"]] == [
        "今天天气很",
        "天气很好",
        "公园里有很多人",
    ]
