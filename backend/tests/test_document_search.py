"""文案搜索：标题与正文都要能命中。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Document, TranscriptSegment
from app.main import app


def test_search_matches_title_and_body(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()

    with SessionLocal() as session:
        by_title = Document(
            title="摆摊创业的三十天复盘",
            platform="kuaishou",
            source_type="url",
            source_value="https://v.kuaishou.com/aaa",
            status="draft",
        )
        by_body = Document(
            title="一条无关的标题",
            platform="bilibili",
            source_type="url",
            source_value="https://www.bilibili.com/video/BV1body",
            status="draft",
        )
        other = Document(
            title="另一条无关的文案",
            platform="douyin",
            source_type="url",
            source_value="https://v.douyin.com/bbb",
            status="draft",
        )
        session.add_all([by_title, by_body, other])
        session.flush()
        session.add(
            TranscriptSegment(
                document_id=by_body.id,
                position=0,
                start_ms=0,
                end_ms=2_000,
                raw_text="今天聊聊摆摊的成本",
                text="今天聊聊摆摊的成本",
            )
        )
        session.commit()
        title_id, body_id, other_id = by_title.id, by_body.id, other.id

    with TestClient(app) as client:
        matched = {item["id"] for item in client.get("/api/documents", params={"q": "摆摊"}).json()}
        # 标题命中 + 正文命中，无关文档不出现
        assert matched == {title_id, body_id}
        assert other_id not in matched
