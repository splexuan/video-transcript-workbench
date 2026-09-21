from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), default="未命名文案")
    platform: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    source_type: Mapped[str] = mapped_column(String(16))
    source_value: Mapped[str] = mapped_column(Text)
    # 这条文案是单条提取还是批量提取来的：'single' | 'batch'。与 source_type 区分开——
    # source_type 说的是来源形态（链接 / 本地文件），这里说的是用户当初怎么提交的。
    source_kind: Mapped[str] = mapped_column(String(16), default="single")
    status: Mapped[str] = mapped_column(String(24), default="processing", index=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 来源作品的元信息：作者、作品介绍，以及封面图文件名（文件在数据目录的 covers/ 下）
    uploader: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_file: Mapped[str | None] = mapped_column(String(120), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    segments: Mapped[list[TranscriptSegment]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="TranscriptSegment.position",
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="document")

    @property
    def has_cover(self) -> bool:
        """对外只说明「有没有封面」，封面文件名属于实现细节，不出后端。"""

        return self.cover_file is not None


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (Index("ix_segment_document_position", "document_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    document: Mapped[Document] = relationship(back_populates="segments")


class JobBatch(Base):
    __tablename__ = "job_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), default="批量提取")
    kind: Mapped[str] = mapped_column(String(16), default="url")
    control_status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    expected_count: Mapped[int] = mapped_column(Integer)
    client_request_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    parent_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    jobs: Mapped[list[Job]] = relationship(
        back_populates="batch",
        order_by="Job.batch_position",
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_job_batch_position", "batch_id", "batch_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    batch_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    platform: Mapped[str] = mapped_column(String(32), default="unknown")
    source_type: Mapped[str] = mapped_column(String(16))
    source_value: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16), default="auto")
    # 用户指定的识别模型（在首页选择）；为空时按 mode 走默认路由。
    requested_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 是否优先使用平台字幕；关闭时跳过字幕，始终用所选模型重新转写。
    prefer_subtitle: Mapped[bool] = mapped_column(Boolean, default=True)
    # 实际使用的识别模型；走平台原生字幕、没有调用模型时为空。
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 文案来源：subtitle（平台/已上传字幕）或 asr（本地识别）。
    transcript_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="waiting")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(500), default="等待处理")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    document: Mapped[Document | None] = relationship(back_populates="jobs")
    batch: Mapped[JobBatch | None] = relationship(back_populates="jobs")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
