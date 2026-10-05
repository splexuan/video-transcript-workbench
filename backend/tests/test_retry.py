"""任务重试的测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.services import create_job
from app.config import settings
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import Job
from app.main import app
from app.schemas import JobCreate


def _seed(status: str = "failed", source_type: str = "url") -> str:
    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(
                source_type=source_type,
                source="https://www.bilibili.com/video/BV1retry"
                if source_type == "url"
                else "D:/media/example.mp4",
                mode="accurate",
                model_id=None,
                prefer_subtitle=False,
            ),
        )
        job.status = status
        job.error_code = "PROCESSING_ERROR" if status == "failed" else None
        job.error_message = "识别失败" if status == "failed" else None
        session.commit()
        return job.id


def test_retry_failed_job_copies_options(tmp_path, monkeypatch) -> None:
    """失败任务可以按原参数重排队：链接、模式、字幕偏好都要带过来。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    job_id = _seed()

    with TestClient(app) as client:
        response = client.post(f"/api/jobs/{job_id}/retry")

    assert response.status_code == 201
    created = response.json()
    assert created["id"] != job_id
    assert created["status"] == "queued"
    assert created["source_value"] == "https://www.bilibili.com/video/BV1retry"
    assert created["mode"] == "accurate"
    # prefer_subtitle 不在列表响应里，直接查库确认原样带过来了
    with SessionLocal() as session:
        retried = session.scalar(select(Job).where(Job.id == created["id"]))
        assert retried is not None
        assert retried.prefer_subtitle is False
        # 原任务保持不变，便于在队列里对照
        assert session.get(Job, job_id).status == "failed"


def test_retry_rejects_running_and_file_sources(tmp_path, monkeypatch) -> None:
    """进行中的任务无需重试；本地文件已被清理，也不能重试。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    running_id = _seed(status="running")
    file_id = _seed(source_type="file")

    with TestClient(app) as client:
        assert client.post(f"/api/jobs/{running_id}/retry").status_code == 400
        file_response = client.post(f"/api/jobs/{file_id}/retry")
        assert file_response.status_code == 400
        assert "重新导入" in file_response.json()["detail"]
        assert client.post("/api/jobs/not-exist/retry").status_code == 404
