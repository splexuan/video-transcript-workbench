"""文案库的组织维度：按作者浏览、关键词覆盖作者与简介、按字数排序。

这一组用例都要断言「全库」的行为（排序、聚合、作者列表封顶），而测试库是同一次
运行共用的一个临时目录，别的用例留下的文案会把结果搅乱，所以每条先清空文案库。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.application import services
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Document, Job, TranscriptSegment
from app.main import app


def reset_documents(session) -> None:
    """清空文案库（含分段，以及挂在文案上的任务记录）。"""

    session.execute(delete(Job).where(Job.document_id.is_not(None)))
    session.execute(delete(TranscriptSegment))
    session.execute(delete(Document))
    session.commit()


def link_subtitle_job(session, document_id: str) -> None:
    """给文案挂一条「平台字幕来源」的任务：批量导出据此改成一行一句。"""

    session.add(
        Job(
            document_id=document_id,
            platform="bilibili",
            source_type="url",
            source_value="https://www.bilibili.com/video/BVsubtitle",
            status="completed",
            transcript_source="subtitle",
        )
    )
    session.commit()


def seed(
    session,
    *,
    title: str,
    uploader: str | None = None,
    description: str | None = None,
    words: int = 0,
) -> str:
    """落一条文案；返回 id。"""

    document = Document(
        title=title,
        platform="bilibili",
        source_type="url",
        source_value=f"https://www.bilibili.com/video/{title}",
        status="draft",
        uploader=uploader,
        description=description,
        word_count=words,
    )
    session.add(document)
    session.commit()
    return document.id


def test_author_filter_scopes_to_the_whole_library() -> None:
    """按作者筛的是全库，不是「当前这一页」——每页只放 2 条也要能筛全。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        expected = {
            seed(session, title=f"牛{i}", uploader="牛老师", words=i) for i in range(3)
        }
        for i in range(5):
            seed(session, title=f"别{i}", uploader="别的作者")

    with TestClient(app) as client:
        first = client.get(
            "/api/documents", params={"uploader": "牛老师", "limit": 2}
        ).json()
        assert first["next_cursor"], "该作者还有没返回的文案，应该给游标"
        second = client.get(
            "/api/documents",
            params={"uploader": "牛老师", "limit": 2, "cursor": first["next_cursor"]},
        ).json()

    assert all(item["uploader"] == "牛老师" for item in first["items"])
    assert {item["id"] for item in first["items"]} | {
        item["id"] for item in second["items"]
    } == expected
    assert second["next_cursor"] is None


def test_search_matches_author_and_description() -> None:
    """关键词不只搜标题和正文：作者名、作品简介也要能搜到。

    用户记得的往往是「那个讲摆摊的博主」，而不是标题里的某句话。
    """

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        by_author = seed(session, title="步数打卡", uploader="牛老师")
        by_description = seed(
            session, title="出摊日记", uploader="另一个人", description="记录摆摊日常"
        )
        seed(session, title="无关文案", uploader="第三个人", description="和关键词无关")

    with TestClient(app) as client:
        author_hit = client.get("/api/documents", params={"q": "牛老师"}).json()
        description_hit = client.get("/api/documents", params={"q": "摆摊日常"}).json()

    assert {item["id"] for item in author_hit["items"]} == {by_author}
    assert {item["id"] for item in description_hit["items"]} == {by_description}


def test_sort_by_words_paginates_without_gaps_or_duplicates() -> None:
    """按字数排全库并翻页：不重不漏。

    这一条专门盯游标的解码：排序键不是时间而是字数，游标里存的就是数字字符串。
    数据里故意留了几组同字数——只按字数排的话同分行的顺序不确定，翻页会重复或漏。
    """

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        expected = {
            seed(session, title=f"文案{index}", words=words)
            for index, words in enumerate([300, 100, 300, 100, 200, 300, 100])
        }

    with TestClient(app) as client:
        fetched: list[str] = []
        counts: list[int] = []
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, object] = {"sort": "words", "limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = client.get("/api/documents", params=params).json()
            fetched.extend(item["id"] for item in body["items"])
            counts.extend(item["word_count"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

    assert cursor is None, "翻到最后一页时游标必须是 None"
    assert len(fetched) == len(set(fetched)), "同一行被返回了两次"
    assert set(fetched) == expected
    assert counts == sorted(counts, reverse=True), "排序键必须单调不增"


def test_unknown_sort_is_rejected() -> None:
    """排序走白名单：不接受列名，也不静默退回默认值。"""

    with TestClient(app) as client:
        response = client.get("/api/documents", params={"sort": "word_count"})

    assert response.status_code == 400


def test_author_filter_combines_with_word_sort() -> None:
    """「看全这个博主的文案、按字数排」是主要使用路径，组合起来也要对。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        expected = {
            seed(session, title=f"我{i}", uploader="牛老师", words=words)
            for i, words in enumerate([10, 500, 80])
        }
        # 别人的文案字数远大于这位作者：筛选一旦失效，它们会排在最前面
        for i in range(3):
            seed(session, title=f"别{i}", uploader="别人", words=9999)

    with TestClient(app) as client:
        body = client.get(
            "/api/documents",
            params={"uploader": "牛老师", "sort": "words", "limit": 10},
        ).json()

    assert [item["word_count"] for item in body["items"]] == [500, 80, 10]
    assert {item["id"] for item in body["items"]} == expected


def test_author_browse_aggregates_and_skips_documents_without_one() -> None:
    """作者视图：每位作者一条，带篇数、总字数与时间跨度；没有作者的文案不进列表。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        seed(session, title="牛1", uploader="牛老师", words=100)
        seed(session, title="牛2", uploader="牛老师", words=250)
        seed(session, title="巧1", uploader="好想吃巧克力", words=50)
        # 本地文件没有作者：归进一个空名字的分组没有意义，不该出现在列表里
        seed(session, title="本地文件", words=999)

    with TestClient(app) as client:
        body = client.get("/api/documents/authors").json()

    assert [item["name"] for item in body["items"]] == ["牛老师", "好想吃巧克力"]
    assert body["total"] == 2
    top = body["items"][0]
    assert (top["count"], top["total_words"]) == (2, 350)
    assert top["first_at"] and top["latest_at"]


def test_author_browse_caps_the_list_and_finds_the_rest_by_name() -> None:
    """作者列表封顶，但按名字搜仍然能找到被截断的作者。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        for i in range(3):
            seed(session, title=f"多{i}", uploader="高产作者", words=10)
        for i in range(2):
            seed(session, title=f"中{i}", uploader="中等作者", words=10)
        # 这一位篇数最少，会落在封顶之外
        seed(session, title="少", uploader="牛老师", words=10)

    with TestClient(app) as client:
        capped = client.get("/api/documents/authors", params={"limit": 2}).json()
        searched = client.get("/api/documents/authors", params={"q": "牛老师"}).json()

    assert capped["total"] == 3, "总数要如实回给界面，好说明只显示了前几位"
    assert [item["name"] for item in capped["items"]] == ["高产作者", "中等作者"]
    assert [item["name"] for item in searched["items"]] == ["牛老师"]


def test_authors_route_is_not_shadowed_by_document_detail() -> None:
    """`/documents/authors` 不能被 `/documents/{document_id}` 抢走。"""

    with TestClient(app) as client:
        response = client.get("/api/documents/authors")

    assert response.status_code == 200
    assert set(response.json()) == {"items", "total"}


@pytest.mark.parametrize("file_format", ["txt", "srt", "vtt", "json"])
def test_batch_export_matches_single_export_byte_for_byte(file_format: str) -> None:
    """批量导出的每个文件必须和「单篇导出」逐字节一致。

    两条路径走的是同一份导出逻辑（含 txt / srt 的 BOM、字幕来源的一行一句），
    谁单独改了另一条路径，这里就会对不上。所以两种来源各放一篇一起比。
    """

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        plain = seed(session, title="普通文案", uploader="牛老师")
        subtitle = seed(session, title="字幕文案", uploader="牛老师")
        link_subtitle_job(session, subtitle)
        for document_id, text in ((plain, "第一句。第二句。"), (subtitle, "一行一句")):
            session.add(
                TranscriptSegment(
                    document_id=document_id, position=0, text=text, raw_text=text
                )
            )
        session.commit()

    with TestClient(app) as client:
        bundle = client.get("/api/documents/export", params={"format": file_format})
        assert bundle.status_code == 200
        assert bundle.headers["content-type"].startswith("application/zip")
        archive = zipfile.ZipFile(io.BytesIO(bundle.content))
        assert sorted(archive.namelist()) == [f"字幕文案.{file_format}", f"普通文案.{file_format}"]
        for document_id, name in ((plain, f"普通文案.{file_format}"), (subtitle, f"字幕文案.{file_format}")):
            single = client.get(
                f"/api/documents/{document_id}/export", params={"format": file_format}
            )
            assert archive.read(name) == single.content, f"{name} 与单篇导出不一致"


def test_batch_export_uses_the_same_filters_as_the_list() -> None:
    """导出的范围必须跟列表的筛选一致：按作者导就只该作者，关键词同理。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        seed(session, title="我的文案", uploader="牛老师")
        seed(session, title="别人的文案", uploader="别人")

    with TestClient(app) as client:
        by_author = zipfile.ZipFile(
            io.BytesIO(client.get("/api/documents/export", params={"uploader": "牛老师"}).content)
        )
        by_keyword = zipfile.ZipFile(
            io.BytesIO(client.get("/api/documents/export", params={"q": "别人"}).content)
        )

    assert by_author.namelist() == ["我的文案.txt"]
    assert by_keyword.namelist() == ["别人的文案.txt"]


def test_batch_export_rejects_empty_selection() -> None:
    """筛出来是空的要给明确提示，而不是丢一个空压缩包给用户。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)

    with TestClient(app) as client:
        response = client.get("/api/documents/export", params={"uploader": "查无此人"})

    assert response.status_code == 400
    assert "没有可导出的文案" in response.json()["detail"]


def test_batch_export_refuses_to_truncate_quietly(monkeypatch) -> None:
    """超过单次上限时明确报错，不能悄悄少打包几篇。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        for index in range(3):
            seed(session, title=f"文案{index}", uploader="牛老师")

    monkeypatch.setattr(services, "MAX_EXPORT_DOCUMENTS", 2)
    with TestClient(app) as client:
        response = client.get("/api/documents/export", params={"uploader": "牛老师"})

    assert response.status_code == 400
    assert "最多导出 2 篇" in response.json()["detail"]


def test_batch_export_keeps_identically_titled_files_apart() -> None:
    """同名、且标题里带路径非法字符时，压缩包里的文件不能互相覆盖。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        seed(session, title="周报 1/2", uploader="牛老师")
        seed(session, title="周报 1/2", uploader="牛老师")

    with TestClient(app) as client:
        archive = zipfile.ZipFile(io.BytesIO(client.get("/api/documents/export").content))

    assert sorted(archive.namelist()) == ["周报 1_2 (2).txt", "周报 1_2.txt"]


def test_unknown_export_format_is_rejected() -> None:
    """格式走白名单：不支持的要 400，而不是给一个坏压缩包。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        seed(session, title="文案", uploader="牛老师")

    with TestClient(app) as client:
        response = client.get("/api/documents/export", params={"format": "docx"})

    assert response.status_code == 400
    assert "不支持的导出格式" in response.json()["detail"]


def test_batch_export_can_pick_specific_documents() -> None:
    """勾选导出：只打包指定的那几篇，跨作者也行。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        picked = [seed(session, title="选中的甲", uploader="牛老师"), seed(session, title="选中的乙", uploader="别人")]
        seed(session, title="没选中的", uploader="牛老师")

    with TestClient(app) as client:
        response = client.get(
            "/api/documents/export",
            params=[("ids", picked[0]), ("ids", picked[1])],
        )
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert sorted(archive.namelist()) == ["选中的乙.txt", "选中的甲.txt"]
        # 勾选导出同样要和单篇导出对得上，不能是另一套拼装
        single = client.get(f"/api/documents/{picked[0]}/export", params={"format": "txt"})
        assert archive.read("选中的甲.txt") == single.content


def test_batch_export_prefers_ids_over_filters() -> None:
    """同时给了勾选和筛选时以勾选为准：id 已经是明确指定，再叠筛选只会让人猜。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        mine = seed(session, title="我的", uploader="牛老师")
        other = seed(session, title="别人的", uploader="别人")

    with TestClient(app) as client:
        response = client.get(
            "/api/documents/export",
            params={
                "ids": [other],
                "uploader": "牛老师",
            },
        )

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == ["别人的.txt"]
    assert mine not in archive.namelist()


def test_batch_export_deduplicates_repeated_ids() -> None:
    """同一个 id 传两次，压缩包里也只该有一份。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        only = seed(session, title="唯一", uploader="牛老师")

    with TestClient(app) as client:
        response = client.get(
            "/api/documents/export", params=[("ids", only), ("ids", only)]
        )

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == ["唯一.txt"]


def test_batch_export_rejects_too_many_selected(monkeypatch) -> None:
    """勾选数量超上限要明确报错，并提示改用筛选导出。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        ids = [seed(session, title=f"勾选{i}", uploader="牛老师") for i in range(3)]

    monkeypatch.setattr(services, "MAX_EXPORT_SELECTION", 2)
    with TestClient(app) as client:
        response = client.get(
            "/api/documents/export",
            params=[("ids", item) for item in ids],
        )

    assert response.status_code == 400
    assert "最多勾选导出 2 篇" in response.json()["detail"]


def test_batch_export_survives_selection_deleted_elsewhere() -> None:
    """勾选的文案里有已经被删掉的：导出剩下的那些，而不是整个失败。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)
        alive = seed(session, title="还在", uploader="牛老师")

    with TestClient(app) as client:
        partial = client.get(
            "/api/documents/export",
            params=[("ids", alive), ("ids", "9f0d2e6a-0000-4000-8000-000000000000")],
        )

    assert partial.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(partial.content)).namelist() == ["还在.txt"]


def test_batch_export_reports_when_whole_selection_is_gone() -> None:
    """勾选的文案全都没了：明确提示去刷新，而不是给一个空压缩包。"""

    init_database()
    with SessionLocal() as session:
        reset_documents(session)

    with TestClient(app) as client:
        response = client.get(
            "/api/documents/export",
            params={"ids": "9f0d2e6a-0000-4000-8000-000000000000"},
        )

    assert response.status_code == 400
    assert "都已不存在" in response.json()["detail"]
