"""列表接口的游标分页：不重不漏，翻页途中插入新行也不打乱后续页。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Document
from app.main import app


def seed_documents(count: int) -> list[str]:
    """直接落库若干文案；返回它们的 id（顺序即创建顺序）。"""

    init_database()
    with SessionLocal() as session:
        documents = [
            Document(
                title=f"分页文案 {index:02d}",
                platform="bilibili",
                source_type="url",
                source_value=f"https://www.bilibili.com/video/BVpage{index}",
                status="draft",
            )
            for index in range(count)
        ]
        session.add_all(documents)
        session.commit()
        return [document.id for document in documents]


def test_cursor_paging_covers_every_row_once() -> None:
    """按游标逐页翻到底：每条恰好出现一次，顺序是「最近更新在前」。"""

    ids = seed_documents(5)
    fetched: list[str] = []
    stamps: list[str] = []
    with TestClient(app) as client:
        cursor: str | None = None
        # 上限只是防死循环；正常应该在游标变成 None 时跳出
        for _ in range(20):
            params: dict[str, object] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = client.get("/api/documents", params=params).json()
            fetched.extend(item["id"] for item in body["items"])
            stamps.extend(item["updated_at"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

    assert cursor is None, "翻到最后一页时游标必须是 None"
    assert len(fetched) == len(set(fetched)), "同一行被返回了两次"
    assert set(ids) <= set(fetched)
    # ISO 时间里同格式的字符串可以直接比大小；倒序分页必须单调不增
    assert stamps == sorted(stamps, reverse=True)


def test_cursor_paging_ignores_rows_inserted_after_the_first_page() -> None:
    """翻页途中插入的新行不会打乱后续页。

    offset 分页在这里会出错：新行把整体往后挤，第 2 页会重复第 1 页的末尾。
    """

    seed_documents(4)
    with TestClient(app) as client:
        first = client.get("/api/documents", params={"limit": 2}).json()
        first_ids = [item["id"] for item in first["items"]]
        assert first["next_cursor"], "后面还有数据，应该给出下一页游标"

        # 第 1 页之后又写入了新文案
        seed_documents(1)
        second = client.get(
            "/api/documents",
            params={"limit": 2, "cursor": first["next_cursor"]},
        ).json()

    second_ids = [item["id"] for item in second["items"]]
    assert len(second_ids) == 2
    assert not set(first_ids) & set(second_ids), "第二页重复返回了第一页已有的行"


def test_invalid_cursor_is_rejected() -> None:
    """游标被改坏时给 400，而不是静默返回错误的一页。"""

    with TestClient(app) as client:
        response = client.get("/api/documents", params={"cursor": "not-a-cursor"})

    assert response.status_code == 400


def test_three_lists_share_the_same_envelope() -> None:
    """任务、批次、文案三个列表用同一种分页形状，前端只按一种结构解析。"""

    with TestClient(app) as client:
        jobs = client.get("/api/jobs", params={"limit": 1}).json()
        batches = client.get("/api/job-batches", params={"limit": 1}).json()
        documents = client.get("/api/documents", params={"limit": 1}).json()

    for body in (jobs, batches, documents):
        assert set(body) == {"items", "next_cursor"}


def test_platform_filter_applies_before_paging() -> None:
    """平台筛选在服务端生效：筛的是全库里该平台的，而不是「当前这一页里的」。"""

    ids = seed_documents(3)
    with TestClient(app) as client:
        filtered = client.get(
            "/api/documents",
            params={"platform": "bilibili", "limit": 100},
        ).json()

    assert filtered["items"], "应有 bilibili 文案"
    assert all(item["platform"] == "bilibili" for item in filtered["items"])
    assert set(ids) <= {item["id"] for item in filtered["items"]}


def test_document_titles_returns_only_requested_ids() -> None:
    """标题接口只回 id 与标题，并且不会被 /documents/{id} 那条路由抢走。"""

    ids = seed_documents(3)
    with TestClient(app) as client:
        body = client.get(
            "/api/documents/titles",
            params=[("ids", ids[0]), ("ids", ids[1])],
        ).json()
        empty = client.get("/api/documents/titles").json()

    assert {item["id"] for item in body} == {ids[0], ids[1]}
    assert all(set(item) == {"id", "title"} for item in body)
    assert empty == []
