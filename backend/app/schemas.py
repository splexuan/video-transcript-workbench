from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页响应：一页数据 + 下一页游标。

    `next_cursor` 为 None 表示已经到底。前端不要自己拼游标，原样回传即可。
    """

    items: list[T]
    next_cursor: str | None = None


class JobCreate(BaseModel):
    source_type: Literal["url", "file"]
    source: str = Field(min_length=1, max_length=4000)
    mode: Literal["auto", "fast", "accurate"] = "auto"
    # 指定用哪个识别模型（首页选择）；留空时按 mode 自动挑选。
    model_id: str | None = Field(default=None, max_length=64)
    # 是否优先使用平台字幕；关掉后跳过字幕，始终用所选模型重新转写。
    prefer_subtitle: bool = True

    @field_validator("source")
    @classmethod
    def strip_source(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("链接或文件路径不能为空")
        return value


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str | None
    batch_id: str | None
    batch_position: int | None
    display_name: str | None
    platform: str
    source_type: str
    source_value: str
    mode: str
    requested_model_id: str | None
    model_id: str | None
    model_name: str | None
    transcript_source: str | None
    status: str
    stage: str
    progress: int
    message: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class JobBatchPreflightRequest(BaseModel):
    sources: list[str] = Field(min_length=1, max_length=50)


class JobBatchCreate(JobBatchPreflightRequest):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    mode: Literal["auto", "fast", "accurate"] = "auto"
    model_id: str | None = Field(default=None, max_length=64)
    prefer_subtitle: bool = True
    client_request_id: str = Field(min_length=8, max_length=100)


class JobBatchPreflightItem(BaseModel):
    position: int
    raw_source: str
    normalized_source: str | None
    platform: str
    status: Literal["valid", "duplicate", "unsupported"]
    message: str


class JobBatchPreflightRead(BaseModel):
    total_count: int
    valid_count: int
    duplicate_count: int
    unsupported_count: int
    can_submit: bool
    items: list[JobBatchPreflightItem]


class JobBatchRead(BaseModel):
    id: str
    title: str
    kind: str
    control_status: str
    status: str
    progress: int
    total_count: int
    queued_count: int
    running_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    parent_batch_id: str | None
    created_at: datetime
    updated_at: datetime


class JobBatchDetailRead(JobBatchRead):
    jobs: list[JobRead]


class SegmentWrite(BaseModel):
    id: int | None = None
    position: int = Field(ge=0)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    text: str = ""


class SegmentRead(SegmentWrite):
    model_config = ConfigDict(from_attributes=True)

    id: int
    raw_text: str


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    platform: str
    source_type: str
    source_value: str
    # 'single' | 'batch'：这条文案来自单条提取还是批量提取
    source_kind: str = "single"
    status: str
    duration_seconds: float | None
    word_count: int
    # 来源作品的元信息：作者与封面（封面由 /api/documents/{id}/cover 提供）
    uploader: str | None = None
    has_cover: bool = False
    created_at: datetime
    updated_at: datetime


class DocumentTitleRead(BaseModel):
    """只有 id 与标题的轻量表示，给任务/批次行反查标题用。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str


class DocumentDetail(DocumentRead):
    segments: list[SegmentRead]
    # 来源作品介绍，通常比标题长，只在详情里给
    description: str | None = None
    # 文案来源：subtitle（平台字幕）或 asr（本地识别）。字幕没有标点，前端据此
    # 决定全文是「一行一句」还是合并成段落。
    transcript_source: str | None = None
    # 原始音轨是否还留在本机（由设置里的「保留原始音视频」决定）；
    # 没保留时前端不渲染播放器，也不要去请求媒体接口。
    media_available: bool = False


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    status: Literal["processing", "draft", "reviewed", "exported"] | None = None


class SegmentsReplace(BaseModel):
    segments: list[SegmentWrite]


class SettingPatch(BaseModel):
    theme: Literal["system", "light", "dark"] | None = None
    default_model: str | None = Field(default=None, max_length=64)
    prefer_subtitle: bool | None = None
    keep_media: bool | None = None
    storage_path: str | None = Field(default=None, max_length=1000)
    # 兜底解析接口的 API Key：只写不读（保存后接口只回传「是否已配置」），空串表示清除
    fallback_api_key: str | None = Field(default=None, max_length=200)


class CookieWrite(BaseModel):
    """用户粘贴的 Cookie 原文；后端只保存加密结果，永不回传内容。"""

    cookie: str = Field(min_length=1, max_length=200_000)


class CookieStatusRead(BaseModel):
    platform: str
    configured: bool
    entries: int
    updated_at: float | None
    # False 表示该平台的凭据可选：不配置也能用，配置后支持会员或登录可见内容。
    required: bool = True


class LoginStatusRead(BaseModel):
    """浏览器助手会话状态；获取成功后 Cookie 会直接写入加密存储。"""

    platform: str
    mode: Literal["guest", "login"]
    status: Literal["idle", "running", "saved", "failed", "cancelled"]
    message: str
    entries: int


class InstallProgressRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model_id: str
    action: str
    status: str
    percent: int
    downloaded_bytes: int
    total_bytes: int
    file_name: str
    completed_files: int
    total_files: int
    message: str
    error: str | None
    updated_at: float


class ModelStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    engine: str
    tier: str
    description: str
    languages: str
    recommended: bool
    note: str | None
    state: Literal["ready", "missing", "partial", "broken", "installing"]
    path: str | None
    installed_bytes: int
    approx_bytes: int
    missing_files: list[str]
    message: str
    source: str | None
    engine_ready: bool
    progress: InstallProgressRead | None


class EngineStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    engine: str
    package_ready: bool
    model_ready: bool
    ready: bool
    active_model: str | None
    specs: list[str]


class ModelStorageRead(BaseModel):
    models_root: str
    total_bytes: int
    disk_free_bytes: int | None


class ModelCatalogRead(BaseModel):
    storage: ModelStorageRead
    engines: list[EngineStatusRead]
    models: list[ModelStatusRead]
    active_tasks: list[InstallProgressRead]
