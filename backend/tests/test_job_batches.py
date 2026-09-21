from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, update

from app.application.services import create_job, create_job_batch, delete_document, pause_job_batch
from app.application.worker import LocalWorker
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
