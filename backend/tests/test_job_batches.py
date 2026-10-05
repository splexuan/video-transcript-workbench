from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, inspect, update

from app.application import services
from app.application.services import (
    cancel_job_batch,
    create_job,
    create_job_batch,
    delete_document,
    list_job_batches,
    pause_job_batch,
)
from app.application.worker import LocalWorker, create_document
from app.domain import MediaInfo
from app.infrastructure import database as database_module
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Document, Job, JobBatch
from app.main import app
from app.schemas import JobBatchCreate, JobCreate


def batch_payload(*sources: str, request_id: str | None = None) -> dict[str, object]:
    return {
        "title": "本周素材",
        "sources": list(sources),
        "mode": "fast",
        "model_id": "sensevoice-small",
        "prefer_subtitle": False,
        "client_request_id": request_id or str(uuid4()),
    }


def reset_batches(session) -> None:
    """清空批次（含子任务）。

    下面几条用例要遍历**整个**批次列表来验证翻页，而测试库是同一次运行共用的一个
    临时目录，别的用例留下的批次会把页翻乱，所以先清干净。
    """
    session.execute(delete(Job).where(Job.batch_id.is_not(None)))
    session.execute(delete(JobBatch))
    session.commit()


def make_batch(session, status: str, marker: str) -> str:
    """造一个状态确定的批次。

    批次状态是从子任务汇总出来的（见 services.batch_status），所以要做成某个状态，
    得摆好子任务的状态，而不是直接给批次写一个字段。
    """
    sources = [f"https://www.bilibili.com/video/BV1{marker}a"]
    if status == "partial_failed":
        sources.append(f"https://www.bilibili.com/video/BV1{marker}b")
    batch = create_job_batch(
        session,
        JobBatchCreate(sources=sources, mode="fast", client_request_id=str(uuid4())),
    )
    if status == "completed":
        for job in batch.jobs:
            job.status = "completed"
    elif status == "failed":
        for job in batch.jobs:
            job.status = "failed"
    elif status == "partial_failed":
        batch.jobs[0].status = "completed"
        batch.jobs[1].status = "failed"
    elif status == "cancelled":
        cancel_job_batch(session, batch)
    elif status != "queued":
        raise AssertionError(f"用例没准备「{status}」的子任务摆法")
    session.commit()
    return batch.id


def test_batch_preflight_marks_duplicate_and_unsupported_lines() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/job-batches/preflight",
            json={
                "sources": [
                    "复制文案 https://b23.tv/abc 好看",
                    "https://b23.tv/abc",
                    "https://example.com/video",
                ]
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["can_submit"] is False
    assert body["valid_count"] == 1
    assert body["duplicate_count"] == 1
    assert body["unsupported_count"] == 1
    assert [item["status"] for item in body["items"]] == [
        "valid",
        "duplicate",
        "unsupported",
    ]


def test_batch_creation_is_atomic_and_idempotent() -> None:
    request_id = str(uuid4())
    payload = batch_payload(
        "https://www.bilibili.com/video/BV1batch",
        "https://v.douyin.com/batch/",
        request_id=request_id,
    )
    with TestClient(app) as client:
        created = client.post("/api/job-batches", json=payload)
        replayed = client.post("/api/job-batches", json=payload)
        conflicted = client.post(
            "/api/job-batches",
            json=batch_payload("https://v.kuaishou.com/other", request_id=request_id),
        )

    assert created.status_code == 201
    assert replayed.status_code == 201
    assert conflicted.status_code == 409
    body = created.json()
    assert replayed.json()["id"] == body["id"]
    assert body["total_count"] == 2
    assert body["status"] == "queued"
    assert [job["batch_position"] for job in body["jobs"]] == [1, 2]
    assert all(job["batch_id"] == body["id"] for job in body["jobs"])


def test_batch_creation_rejects_any_invalid_line_without_writing() -> None:
    request_id = str(uuid4())
    with TestClient(app) as client:
        response = client.post(
            "/api/job-batches",
            json=batch_payload(
                "https://www.bilibili.com/video/BV1valid",
                "https://example.com/not-supported",
                request_id=request_id,
            ),
        )
        batches = client.get("/api/job-batches?limit=100")

    assert response.status_code == 422
    assert batches.status_code == 200
    with SessionLocal() as session:
        assert (
            session.query(JobBatch)
            .filter(JobBatch.client_request_id == request_id)
            .count()
            == 0
        )
        assert session.query(Job).filter(Job.source_value == "https://example.com/not-supported").count() == 0


def test_batch_pause_resume_cancel_and_retry_failed() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/job-batches",
            json=batch_payload(
                "https://www.bilibili.com/video/BV1control",
                "https://v.douyin.com/control/",
            ),
        ).json()
        batch_id = created["id"]
        child_id = created["jobs"][0]["id"]
        assert client.post(f"/api/jobs/{child_id}/cancel").status_code == 400
        assert client.post(f"/api/jobs/{child_id}/retry").status_code == 400
        assert client.delete(f"/api/jobs/{child_id}").status_code == 400
        paused = client.post(f"/api/job-batches/{batch_id}/pause")
        resumed = client.post(f"/api/job-batches/{batch_id}/resume")

        with SessionLocal() as session:
            first = session.get(Job, created["jobs"][0]["id"])
            second = session.get(Job, created["jobs"][1]["id"])
            assert first is not None and second is not None
            first.status = "failed"
            first.progress = 100
            second.status = "completed"
            second.progress = 100
            session.commit()

        detail = client.get(f"/api/job-batches/{batch_id}")
        ended_cancel = client.post(f"/api/job-batches/{batch_id}/cancel")
        retried = client.post(f"/api/job-batches/{batch_id}/retry-failed")
        cancelled = client.post(f"/api/job-batches/{retried.json()['id']}/cancel")

    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert resumed.status_code == 200
    assert resumed.json()["control_status"] == "active"
    assert detail.json()["status"] == "partial_failed"
    assert ended_cancel.status_code == 400
    assert retried.status_code == 201
    assert retried.json()["parent_batch_id"] == batch_id
    assert retried.json()["total_count"] == 1
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["jobs"][0]["status"] == "cancelled"


def test_worker_skips_jobs_in_a_paused_batch() -> None:
    init_database()
    with SessionLocal() as session:
        session.execute(update(Job).where(Job.status == "queued").values(status="cancelled"))
        batch = create_job_batch(
            session,
            JobBatchCreate(**batch_payload("https://www.bilibili.com/video/BV1paused")),
        )
        paused_job_id = batch.jobs[0].id
        pause_job_batch(session, batch)
        standalone = create_job(
            session,
            JobCreate(source_type="url", source="https://v.douyin.com/standalone/", mode="fast"),
        )
        standalone_id = standalone.id

    claimed_id = LocalWorker()._claim_next()

    assert claimed_id == standalone_id
    with SessionLocal() as session:
        assert session.get(Job, paused_job_id).status == "queued"


def test_deleting_a_document_preserves_its_batch_job_history() -> None:
    init_database()
    with SessionLocal() as session:
        batch = create_job_batch(
            session,
            JobBatchCreate(**batch_payload("https://www.bilibili.com/video/BV1document")),
        )
        job = batch.jobs[0]
        document = Document(
            title="准备删除的文案",
            platform="bilibili",
            source_type="url",
            source_value=job.source_value,
            status="draft",
        )
        session.add(document)
        job.document = document
        job.status = "completed"
        job.progress = 100
        session.commit()
        job_id = job.id

        delete_document(session, document)

        preserved = session.get(Job, job_id)
        assert preserved is not None
        assert preserved.document_id is None
        assert preserved.status == "completed"


def test_additive_migration_upgrades_an_existing_jobs_table(monkeypatch, tmp_path) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE documents (id VARCHAR(36) PRIMARY KEY)")
    monkeypatch.setattr(database_module, "engine", legacy_engine)

    database_module._add_missing_columns()

    inspector = inspect(legacy_engine)
    columns = {column["name"] for column in inspector.get_columns("jobs")}
    indexes = {index["name"] for index in inspector.get_indexes("jobs")}
    assert {"batch_id", "batch_position", "display_name"} <= columns
    assert {"ix_jobs_batch_id", "ix_job_batch_position"} <= indexes


def test_batch_and_job_names_use_short_ids() -> None:
    """队列里的名字与内容解耦：批次名、任务名都是 ID 前缀。

    链接和整段分享文案又长又乱，靠内容推导的名字还会重名（同平台同域名会显示成
    一样的域名），所以取唯一、稳定的短 ID；识别出文案标题后界面会换成真正的标题。
    """

    with TestClient(app) as client:
        created = client.post(
            "/api/job-batches",
            json={
                "sources": [
                    "https://www.bilibili.com/video/BV1cSec6tEux",
                    "https://v.douyin.com/iRxYwX/",
                ],
                "mode": "fast",
                "client_request_id": str(uuid4()),
            },
        )

    assert created.status_code == 201
    body = created.json()
    assert body["title"] == f"批次 {body['id'][:8]}"
    for job in body["jobs"]:
        assert job["display_name"] == job["id"][:8]


def test_single_job_name_is_its_short_id() -> None:
    """单条任务同样用 ID 前缀，而不是那一长串粘贴文案。"""

    with TestClient(app) as client:
        created = client.post(
            "/api/jobs",
            json={
                "source_type": "url",
                "source": "7.43 复制打开抖音 https://v.douyin.com/abc/ 看看",
            },
        )

    assert created.status_code == 201
    body = created.json()
    assert body["display_name"] == body["id"][:8]


def test_local_file_job_keeps_a_readable_name() -> None:
    """本地文件是唯一的例外：不带 display_name，界面按文件名显示更可读。"""

    with TestClient(app), SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="file", source="/uploads/9f2a__我的视频.mp4"),
        )

    assert job.display_name is None


def test_document_records_whether_it_came_from_a_batch() -> None:
    """文案库要能标出这条文案来自单条提取还是批量提取。"""

    init_database()
    with SessionLocal() as session:
        single_job = create_job(
            session,
            JobCreate(source_type="url", source="https://www.bilibili.com/video/BVsingle"),
        )
        batch = create_job_batch(
            session,
            JobBatchCreate(
                sources=["https://www.bilibili.com/video/BV1batch"],
                mode="fast",
                client_request_id=str(uuid4()),
            ),
        )
        single_document = create_document(
            session, single_job, MediaInfo(title="单条", platform="bilibili")
        )
        batch_document = create_document(
            session, batch.jobs[0], MediaInfo(title="批量", platform="bilibili")
        )

    assert single_document.source_kind == "single"
    assert batch_document.source_kind == "batch"


def test_document_origin_backfill_marks_preexisting_batch_documents(
    monkeypatch, tmp_path
) -> None:
    """老库补 source_kind 时：既不能留 NULL，也不能把批量的历史文案标成单条。"""

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-origin.db'}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE jobs ("
            "id VARCHAR(36) PRIMARY KEY, document_id VARCHAR(36), batch_id VARCHAR(36))"
        )
        connection.exec_driver_sql("CREATE TABLE documents (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql(
            "INSERT INTO documents (id) VALUES ('doc-batch'), ('doc-single')"
        )
        connection.exec_driver_sql(
            "INSERT INTO jobs (id, document_id, batch_id) VALUES "
            "('job-1', 'doc-batch', 'batch-1'), ('job-2', 'doc-single', NULL)"
        )
    monkeypatch.setattr(database_module, "engine", legacy_engine)

    database_module._add_missing_columns()
    database_module._backfill_document_source_kind()

    with legacy_engine.begin() as connection:
        rows = dict(
            connection.exec_driver_sql("SELECT id, source_kind FROM documents").all()
        )
    assert rows == {"doc-batch": "batch", "doc-single": "single"}


def test_batch_status_filter_paginates_without_gaps_or_duplicates(monkeypatch) -> None:
    """按状态筛批次：翻页既不能漏也不能重。

    批次状态是算出来的、库里没有这一列，服务端只能边扫边算。这里把扫描分片调得
    比一页还小，逼出两种边界：分片边界落在匹配项之间、以及一个分片里的匹配项比
    一页还多。
    """

    init_database()
    monkeypatch.setattr(services, "_BATCH_SCAN_CHUNK", 3)
    with SessionLocal() as session:
        reset_batches(session)
        expected = {make_batch(session, "completed", f"done{i}") for i in range(5)}
        # 穿插一些不匹配的批次，让分片边界不会正好落在匹配项之间
        for i in range(4):
            make_batch(session, "queued", f"wait{i}")

    with SessionLocal() as session:
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            rows, cursor = list_job_batches(
                session, statuses=["completed"], limit=2, cursor=cursor
            )
            seen.extend(row.id for row in rows)
            pages += 1
            assert pages < 20, "翻页没有收敛"
            if cursor is None:
                break

    assert len(seen) == len(set(seen)), "同一个批次被返回了两次"
    assert set(seen) == expected


def test_batch_status_filter_keeps_matches_sharing_one_chunk(monkeypatch) -> None:
    """一个分片里的匹配项多于一页时，剩下的必须在下一页出现。

    这是这套筛选最容易写错的地方：游标若回传「最后扫过的位置」，同一个分片里
    多出来的匹配项会被直接跳过。
    """

    init_database()
    monkeypatch.setattr(services, "_BATCH_SCAN_CHUNK", 8)
    with SessionLocal() as session:
        reset_batches(session)
        expected = {make_batch(session, "completed", f"same{i}") for i in range(4)}

    with SessionLocal() as session:
        first, cursor = list_job_batches(session, statuses=["completed"], limit=2, cursor=None)
        assert len(first) == 2
        assert cursor is not None, "还有没返回的匹配项，必须给游标"
        second, cursor = list_job_batches(session, statuses=["completed"], limit=2, cursor=cursor)

    assert len(second) == 2
    assert {row.id for row in first} | {row.id for row in second} == expected


def test_batch_status_filter_resumes_after_hitting_the_scan_cap(monkeypatch) -> None:
    """扫到上限也没凑满一页时：返回已凑到的，并给游标——否则剩下的永远看不到。"""

    init_database()
    monkeypatch.setattr(services, "_BATCH_SCAN_CHUNK", 2)
    monkeypatch.setattr(services, "_MAX_BATCH_SCAN", 2)
    with SessionLocal() as session:
        reset_batches(session)
        expected = {make_batch(session, "cancelled", f"cap{i}") for i in range(4)}

    with SessionLocal() as session:
        first, cursor = list_job_batches(session, statuses=["cancelled"], limit=10, cursor=None)
        assert len(first) == 2, "一轮只扫了 2 行"
        assert cursor is not None
        second, cursor = list_job_batches(session, statuses=["cancelled"], limit=10, cursor=cursor)

    assert {row.id for row in first} | {row.id for row in second} == expected
    assert cursor is None, "取完了就该明确收尾"


def test_batch_status_filter_hides_everything_else() -> None:
    """不匹配的批次一条都不能露出来，并且明确收尾（游标为 None）。"""

    init_database()
    with SessionLocal() as session:
        reset_batches(session)
        target = make_batch(session, "failed", "only")
        make_batch(session, "completed", "other")
        make_batch(session, "queued", "pending")

        rows, cursor = list_job_batches(session, statuses=["failed"], limit=10, cursor=None)

    assert [row.id for row in rows] == [target]
    assert cursor is None


def test_batch_status_filter_matches_derived_statuses_over_the_api() -> None:
    """暂停、部分失败这些批次状态不在任务状态集合里，前端按各自的取值筛。"""

    init_database()
    with SessionLocal() as session:
        reset_batches(session)
        paused = make_batch(session, "queued", "pause")
        paused_batch = session.get(JobBatch, paused)
        assert paused_batch is not None
        pause_job_batch(session, paused_batch)
        session.commit()
        partial = make_batch(session, "partial_failed", "partial")
        done = make_batch(session, "completed", "done")

    with TestClient(app) as client:
        # 前端把「进行中」展开成三个值（暂停的批次没结束、还能继续），用重复参数传
        active = client.get("/api/job-batches?status=queued&status=running&status=paused")
        failed = client.get(
            "/api/job-batches?status=failed&status=partial_failed&status=cancelled"
        )
        completed = client.get("/api/job-batches?status=completed")

    assert active.status_code == 200
    assert [item["id"] for item in active.json()["items"]] == [paused]
    assert [item["id"] for item in failed.json()["items"]] == [partial]
    assert [item["id"] for item in completed.json()["items"]] == [done]
