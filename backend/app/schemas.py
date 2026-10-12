from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

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
    # 正文形态：False = 字幕原样一行一句，True = 智能分句整理过的自然段（段落之间空行）。
    # 由保存正文的两个入口各自维护，前端据此决定正文怎么排版。
    paragraph_body: bool = False
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


class AuthorSummary(BaseModel):
    """文案库里的作者聚合：一位作者一条。"""

    name: str
    count: int
    total_words: int
    # 时间跨度：第一次与最近一次提取这个作者的作品
    first_at: datetime
    latest_at: datetime


class AuthorPage(BaseModel):
    """作者列表：封顶返回 + 总数，界面据此说明「只显示了前 N 位」。"""

    items: list[AuthorSummary]
    total: int


class DocumentDetail(DocumentRead):
    segments: list[SegmentRead]
    # 来源作品介绍，通常比标题长，只在详情里给
    description: str | None = None
    # 原始音轨是否还留在本机（由设置里的「保留原始音视频」决定）；
    # 没保留时前端不渲染播放器，也不要去请求媒体接口。
    media_available: bool = False


class DocumentUpdate(BaseModel):
    """可改的文案属性：目前只有标题。"""

    title: str | None = Field(default=None, min_length=1, max_length=300)


class SegmentsReplace(BaseModel):
    segments: list[SegmentWrite]


class BodyWrite(BaseModel):
    """整篇正文：一行一条分段，段落之间以空行分隔（空行只作排版）。"""

    text: str = Field(min_length=1, max_length=2_000_000)


class PolishRead(BaseModel):
    """智能分句的结果：整理后的分段已经落库，附带新文案与界面说明用的统计。"""

    text: str
    # 整理后的自然段数量与句子数量，用于界面上的说明文字
    paragraphs: int
    sentences: int
    # 合并掉的滚动重复条目数（0 表示没有发现重复）
    merged: int
    # 是否调用了标点模型（正文本来就带标点时是 false）
    punctuated: bool
    # 整理直接写回后的文案：前端拿它替换当前文档，不用再走一遍保存
    document: DocumentDetail


class SettingPatch(BaseModel):
    theme: Literal["system", "light", "dark"] | None = None
    default_model: str | None = Field(default=None, max_length=64)
    prefer_subtitle: bool | None = None
    keep_media: bool | None = None
    storage_path: str | None = Field(default=None, max_length=1000)
    # 兜底解析接口的 API Key：只写不读（保存后接口只回传「是否已配置」），空串表示清除
    fallback_api_key: str | None = Field(default=None, max_length=200)
    # 「AI 总结」用哪个驱动：deepseek（跳网页）/ workbuddy / codex（本机登录态）
    ai_provider: Literal["deepseek", "workbuddy", "codex"] | None = None
    # 本机驱动下的对话模型；留空由驱动自己挑默认模型
    ai_model: str | None = Field(default=None, max_length=128)
    # Codex 出网用的代理（本机可在「设置 - AI 驱动」里填）；留空走系统默认
    ai_proxy: str | None = Field(default=None, max_length=500)
    # 「语音合成」默认用的模型与音色（音色 id 是 ZipVoice 的参考音频标识）；留空用引擎默认
    tts_model: str | None = Field(default=None, max_length=64)
    tts_voice: str | None = Field(default=None, max_length=64)


class TtsVoiceItem(BaseModel):
    """一个音色。ZipVoice 是零样本克隆，音色来自一段参考音频，id 即这段音频的标识。"""

    id: str
    name: str
    # 这段参考音频里逐字说出的内容：必须与音频一致，否则克隆出的音色会明显变差
    reference_text: str
    builtin: bool


class TtsVoiceCreate(BaseModel):
    """上传自定义音色时的表单部分（音频本身走 multipart 文件字段）。"""

    name: str = Field(min_length=1, max_length=40)
    reference_text: str = Field(min_length=1, max_length=500)


class TtsTranscribeRead(BaseModel):
    """参考音频的自动转写结果：文字给用户核对，时长用于提示是否截断过。"""

    text: str
    seconds: float
    # 超过可用的参考长度时会被截断，前端要如实告知，否则文字与音频对不上
    truncated: bool = False


class TtsModelOption(BaseModel):
    id: str
    name: str
    state: str
    ready: bool
    approx_bytes: int


class TtsConfigRead(BaseModel):
    """语音合成页的配置：能选什么模型/音色、当前选择、以及估算依据。"""

    ready: bool
    message: str
    model: TtsModelOption | None
    models: list[TtsModelOption]
    voices: list[TtsVoiceItem]
    voice: str
    speed: float
    max_chars: int
    # 语速与音质的关系：int8 版实测 RTF 约 0.5，多实例并行还能再摊薄
    chars_per_second: float
    realtime_factor: float
    parallel_lanes: int


class TtsSynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    # 留空用设置里的默认模型/音色
    model_id: str | None = Field(default=None, max_length=64)
    voice: str | None = Field(default=None, max_length=64)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)


class TtsJobRead(BaseModel):
    """合成任务的状态快照；audio_url 在完成后才给出。"""

    id: str
    status: str
    message: str
    percent: int
    model_id: str
    voice: str
    speed: float
    chars: int
    sentences_done: int
    sentences_total: int
    duration: float
    elapsed: float
    error: str | None
    audio_url: str | None
    created_at: float
    updated_at: float


class AiSummarizeRequest(BaseModel):
    """AI 总结请求：正文由前端给（编辑页可能带着未保存的改动）。"""

    title: str = Field(default="", max_length=300)
    text: str = Field(min_length=1, max_length=200_000)
    # 允许本次临时换驱动/模型；留空用设置里的选择
    provider: Literal["workbuddy", "codex"] | None = None
    model: str | None = Field(default=None, max_length=128)


class AiSummaryRead(BaseModel):
    """AI 总结结果：正文 + 是哪个驱动/模型产出的，便于界面标注来源。"""

    provider: str
    provider_name: str
    model: str
    text: str
    usage: dict[str, Any] = Field(default_factory=dict)


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
