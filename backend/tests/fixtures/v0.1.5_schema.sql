-- v0.1.5（旧仓库 video-transcript-workbench）实际建出的 SQLite 结构。
-- 用于锁定「旧版数据目录直接升级到新版」的兼容契约：
--   * 新版只做加法（补 documents.paragraph_body、新增 tts_jobs 表）
--   * 旧文案、分段、任务、设置必须原样保留
-- 这个文件由 v0.1.5 的应用启动一次后从 sqlite_master 导出，不要手改列定义。

CREATE TABLE app_settings (
    "key" VARCHAR(100) NOT NULL,
    value TEXT NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY ("key")
);

CREATE TABLE documents (
    id VARCHAR(36) NOT NULL,
    title VARCHAR(300) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    source_type VARCHAR(16) NOT NULL,
    source_value TEXT NOT NULL,
    source_kind VARCHAR(16) NOT NULL,
    status VARCHAR(24) NOT NULL,
    duration_seconds FLOAT,
    uploader VARCHAR(200),
    description TEXT,
    cover_file VARCHAR(120),
    word_count INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE job_batches (
    id VARCHAR(36) NOT NULL,
    title VARCHAR(300) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    control_status VARCHAR(16) NOT NULL,
    expected_count INTEGER NOT NULL,
    client_request_id VARCHAR(100) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    parent_batch_id VARCHAR(36),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE jobs (
    id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36),
    batch_id VARCHAR(36),
    batch_position INTEGER,
    display_name VARCHAR(300),
    platform VARCHAR(32) NOT NULL,
    source_type VARCHAR(16) NOT NULL,
    source_value TEXT NOT NULL,
    mode VARCHAR(16) NOT NULL,
    requested_model_id VARCHAR(64),
    prefer_subtitle BOOLEAN NOT NULL,
    model_id VARCHAR(64),
    model_name VARCHAR(120),
    transcript_source VARCHAR(16),
    status VARCHAR(24) NOT NULL,
    stage VARCHAR(32) NOT NULL,
    progress INTEGER NOT NULL,
    message VARCHAR(500) NOT NULL,
    error_code VARCHAR(80),
    error_message TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE SET NULL,
    FOREIGN KEY(batch_id) REFERENCES job_batches (id) ON DELETE SET NULL
);

CREATE TABLE transcript_segments (
    id INTEGER NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    position INTEGER NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    raw_text TEXT NOT NULL,
    text TEXT NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE INDEX ix_documents_created_at ON documents (created_at);
CREATE INDEX ix_documents_platform ON documents (platform);
CREATE INDEX ix_documents_status ON documents (status);
CREATE INDEX ix_job_batch_position ON jobs (batch_id, batch_position);
CREATE UNIQUE INDEX ix_job_batches_client_request_id ON job_batches (client_request_id);
CREATE INDEX ix_job_batches_control_status ON job_batches (control_status);
CREATE INDEX ix_job_batches_created_at ON job_batches (created_at);
CREATE INDEX ix_job_batches_parent_batch_id ON job_batches (parent_batch_id);
CREATE INDEX ix_jobs_batch_id ON jobs (batch_id);
CREATE INDEX ix_jobs_created_at ON jobs (created_at);
CREATE INDEX ix_jobs_document_id ON jobs (document_id);
CREATE INDEX ix_jobs_status ON jobs (status);
CREATE INDEX ix_segment_document_position ON transcript_segments (document_id, position);
CREATE INDEX ix_transcript_segments_document_id ON transcript_segments (document_id);
