"""删除任务记录与文案的测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.application.services import DocumentInUseError, create_job, delete_document
from app.config import settings
from app.infrastructure.cover_store import cover_path
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Document, Job
from app.main import app
from app.schemas import JobCreate


def _make_job(session, **overrides) -> Job:
    job = create_job(
        session,
        JobCreate(
            source_type=overrides.get("source_type", "url"),
            source=overrides.get("source", "https://www.bilibili.com/video/BV1xx"),
            mode="fast",
        ),
    )
    job.status = overrides.get("status", "completed")
    session.commit()
    return job


def test_delete_job_removes_workspace(tmp_path, monkeypatch) -> None:
    """删除终态任务：记录消失，工作目录一并清理；进行中的任务拒绝删除。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()

    with SessionLocal() as session:
        job = _make_job(session)
        workspace = settings.work_dir / job.id
        workspace.mkdir(parents=True)
        (workspace / "audio.wav").write_bytes(b"wav")
        job_id = job.id

    with TestClient(app) as client:
        # 进行中的任务不让直接删
        with SessionLocal() as session:
            running = _make_job(session, status="running")
            running_id = running.id
        assert client.delete(f"/api/jobs/{running_id}").status_code == 400
        assert client.delete(f"/api/jobs/{job_id}").status_code == 200

    assert not workspace.exists()
    with SessionLocal() as session:
        assert session.get(Job, job_id) is None


def test_delete_document_removes_related_job_and_cover(tmp_path, monkeypatch) -> None:
    """删除文案：分段级联清掉，关联任务记录、封面、工作目录一起删。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()

    with SessionLocal() as session:
        job = _make_job(session)
        document = Document(
            title="要删除的文案",
            platform="bilibili",
            source_type="url",
            source_value=job.source_value,
            status="draft",
        )
        session.add(document)
        session.flush()
        job.document_id = document.id
        cover_dir = settings.data_dir / "covers"
        cover_dir.mkdir(parents=True, exist_ok=True)
        document.cover_file = f"{document.id}.png"
        cover_path(document.cover_file).write_bytes(b"png")
        session.commit()
        workspace = settings.work_dir / job.id
        workspace.mkdir(parents=True)
        document_id = document.id

        delete_document(session, session.get(Document, document_id))

        assert session.get(Document, document_id) is None
        assert session.get(Job, job.id) is None
    assert not workspace.exists()
    assert document.cover_file is None or not cover_path(document.cover_file).exists()


def test_delete_document_blocked_while_job_running(tmp_path, monkeypatch) -> None:
    """任务还在处理时不允许删文案，避免工作目录被运行中的任务重建。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()

    with SessionLocal() as session:
        job = _make_job(session, status="running")
        document = Document(
            title="处理中的文案",
            platform="bilibili",
            source_type="url",
            source_value=job.source_value,
            status="processing",
        )
        session.add(document)
        session.flush()
        job.document_id = document.id
        session.commit()

        with pytest.raises(DocumentInUseError):
            delete_document(session, document)
