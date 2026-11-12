"""旧版（v0.1.5）数据目录直接升级到新版的回归测试。

旧仓库 `video-transcript-workbench` 的发行包与新版共用同一个数据目录
（`%LOCALAPPDATA%\\VideoTranscriptWorkbench`）。升级时用户不会导出/导入，
新版必须在原来的 `workbench.db` 上就地补列、建表，并保留全部历史数据。

这里用 v0.1.5 实际导出的表结构造库，不依赖旧仓库代码。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import sessionmaker

from app.application.services import read_settings
from app.config import settings
from app.infrastructure import database as database_module
from app.infrastructure import model_catalog, model_store

FIXTURE = Path(__file__).parent / "fixtures" / "v0.1.5_schema.sql"


def _legacy_database(tmp_path: Path) -> Path:
    path = tmp_path / "workbench.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(FIXTURE.read_text(encoding="utf-8"))
        connection.executescript(
            """
            INSERT INTO app_settings (key, value, updated_at) VALUES
              ('default_model', 'sensevoice-small', '2026-09-01 00:00:00'),
              ('theme', 'dark', '2026-09-01 00:00:00'),
              ('auto_check_update', 'false', '2026-09-01 00:00:00'),
              ('ignored_version', '0.1.4', '2026-09-01 00:00:00');

            INSERT INTO job_batches (
                id, title, kind, control_status, expected_count,
                client_request_id, request_fingerprint, parent_batch_id,
                created_at, updated_at
            ) VALUES (
                'batch-1', '旧版批量', 'url', 'active', 1,
                'client-request-1', 'fingerprint', NULL,
                '2026-09-01 00:00:00', '2026-09-01 00:00:00'
            );

            INSERT INTO documents (
                id, title, platform, source_type, source_value, source_kind,
                status, duration_seconds, uploader, description, cover_file,
                word_count, created_at, updated_at
            ) VALUES (
                'doc-1', '旧版文案', 'bilibili', 'url', 'https://example.test/v',
                'single', 'completed', 120.5, '作者', '作品简介', 'cover.jpg',
                42, '2026-09-01 00:00:00', '2026-09-01 00:00:00'
            );

            INSERT INTO transcript_segments (
                document_id, position, start_ms, end_ms, raw_text, text, updated_at
            ) VALUES (
                'doc-1', 0, 0, 1000, '识别原文', '整理后的正文',
                '2026-09-01 00:00:00'
            );

            INSERT INTO jobs (
                id, document_id, batch_id, batch_position, display_name, platform,
                source_type, source_value, mode, requested_model_id, prefer_subtitle,
                model_id, model_name, transcript_source, status, stage, progress,
                message, error_code, error_message, created_at, updated_at
            ) VALUES (
                'job-1', 'doc-1', 'batch-1', 1, '旧任务', 'bilibili',
                'url', 'https://example.test/v', 'fast', 'sensevoice-small', 1,
                'sensevoice-small', 'SenseVoice Small', 'sensevoice',
                'completed', 'done', 100, '完成', NULL, NULL,
                '2026-09-01 00:00:00', '2026-09-01 00:00:00'
            );
            """
        )
    return path


def test_v015_database_upgrades_in_place(tmp_path, monkeypatch) -> None:
    path = _legacy_database(tmp_path)
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    monkeypatch.setattr(database_module, "engine", engine)

    # 新版启动时做的就是这一件事。
    database_module.init_database()

    inspector = sqlalchemy_inspect(engine)
    assert "tts_jobs" in inspector.get_table_names()
    document_columns = {column["name"] for column in inspector.get_columns("documents")}
    assert {"source_kind", "paragraph_body"} <= document_columns
    tts_columns = {column["name"] for column in inspector.get_columns("tts_jobs")}
    assert "text" in tts_columns

    Session = sessionmaker(bind=engine)
    with Session() as session:
        document = session.execute(
            text(
                "SELECT title, source_kind, paragraph_body, uploader, cover_file "
                "FROM documents WHERE id = 'doc-1'"
            )
        ).one()
        assert document.title == "旧版文案"
        # v0.1.5 的文案本身是单条提取，但关联任务属于批次：按任务回填成 batch。
        assert document.source_kind == "batch"
        # 新列对老行使用默认值：保持旧的逐行展示。
        assert document.paragraph_body == 0
        assert document.uploader == "作者"
        assert document.cover_file == "cover.jpg"

        segment = session.execute(
            text("SELECT text FROM transcript_segments WHERE document_id = 'doc-1'")
        ).scalar_one()
        assert segment == "整理后的正文"

        job = session.execute(
            text("SELECT status, model_id FROM jobs WHERE id = 'job-1'")
        ).one()
        assert (job.status, job.model_id) == ("completed", "sensevoice-small")

        # 旧设置原样读回：包括旧版更新检查用过的两个键。
        payload = read_settings(session)
        assert payload["theme"] == "dark"
        assert payload["default_model"] == "sensevoice-small"
        assert payload["auto_check_update"] is False
        assert payload["ignored_version"] == "0.1.4"


def test_v015_model_directory_is_reused(tmp_path, monkeypatch) -> None:
    """旧版下到数据目录里的模型，新版要能直接认出来，不要求重新下载。"""

    monkeypatch.setattr(settings, "models_dir", tmp_path / "models")
    directory = tmp_path / "models" / "sensevoice-small"
    directory.mkdir(parents=True)
    # v0.1.5 时代下载的量化文件名是 model_q8.onnx；新版规范名是 model.int8.onnx。
    (directory / "model_q8.onnx").write_bytes(b"legacy-model")
    (directory / "tokens.txt").write_bytes(b"tokens")

    spec = model_catalog.require_spec("sensevoice-small")
    status = model_store.inspect(spec)

    assert status.state == "ready"
    assert status.path == str(directory)
