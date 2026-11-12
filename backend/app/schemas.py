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
    # 「语音合成」默认用的模型与音色（音色 id 是 ZipVoice 的参考音频标识）；留空用引擎默认
    tts_model: str | None = Field(default=None, max_length=64)
    tts_voice: str | None = Field(default=None, max_length=64)
    # 版本更新：启动时自动检查；ignored_version 是用户点过「跳过此版本」的版本号
    auto_check_update: bool | None = None
    ignored_version: str | None = Field(default=None, max_length=32)


class TtsVoiceItem(BaseModel):
    """一个音色。ZipVoice 是零样本克隆，音色来自一段参考音频，id 即这段音频的标识。"""

    id: str
    name: str
    # 这段参考音频里逐字说出的内容：必须与音频一致，否则克隆出的音色会明显变差
    reference_text: str
    builtin: bool
    # 参考音频时长（秒）：前端画波形、判断选区是否过短都要它；内置音色为 0
    seconds: float = 0.0
    # 自定义音色的参考音频地址（用于试听与波形编辑）；内置音色为 null
    audio_url: str | None = None


class TtsVoiceTrimRequest(BaseModel):
    """把参考音频裁到 [start, end] 秒。

    `retranscribe` 为真时按裁剪后的音频重新转写参考文本——不动文本的话，
    文本与音频就对不上了，而对不上正是克隆质量下降的主因。
    """

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    reference_text: str | None = Field(default=None, max_length=500)
    retranscribe: bool = False


class TtsVoiceRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class TtsVoicePreviewRequest(BaseModel):
    """按选区重新转写已保存的参考音频（只回填文本，不落盘）。

    两端都不给就等于转写整段，因此允许缺省。
    """

    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)


class TtsVoiceEditRead(BaseModel):
    """一次参考音频改动后的回执：更新后的音色 + 该怎么告诉用户结果。"""

    voice: TtsVoiceItem
    # 参考文本的来源：手填 / 转写 / 沿用旧值
    text_source: str = "typed"
    # 需要如实告知用户的原因（转写失败之类）；成功时为空
    note: str = ""


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
    # 语速与音质的关系：fp32 版、8 步采样实测 RTF 约 1.3，多实例并行还能再摊薄
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
    # 提交时的原文：点历史记录能填回输入框继续改。
    # 早期记录没有存原文，这时是空串，前端据此不覆盖输入框里已有的内容。
    text: str = ""
    chars: int
    sentences_done: int
    sentences_total: int
    duration: float
    elapsed: float
    error: str | None
    audio_url: str | None
    created_at: float
    updated_at: float


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


class SkillFileRead(BaseModel):
    """技能包里的一个可预览文本文件。"""

    path: str
    size: int
    kind: Literal["markdown", "python", "json", "text"]


class SkillRead(BaseModel):
    """可下载的独立技能包；`download_url` 直接给浏览器当下载地址用。"""

    id: str
    title: str
    description: str
    version: str | None
    files: list[SkillFileRead]
    total_bytes: int
    updated_at: float | None
    download_url: str


class SkillListRead(BaseModel):
    items: list[SkillRead]


class SkillFileContentRead(BaseModel):
    path: str
    content: str
    truncated: bool


# ---------------------------------------------------------------- 版本更新


class UpdateAssetRead(BaseModel):
    """Release 里的下载资产：只挑 Windows 免安装包（zip）。"""

    name: str
    size: int
    url: str


class UpdateReleaseRead(BaseModel):
    version: str
    tag: str
    name: str
    # Release 正文（markdown 原文），由界面自行截断展示
    notes: str
    published_at: str
    page_url: str
    asset: UpdateAssetRead | None = None


class UpdateProgressRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: str
    file_name: str
    status: str
    percent: int
    downloaded_bytes: int
    total_bytes: int
    path: str
    message: str
    error: str | None = None
    updated_at: float


class UpdatePackageRead(BaseModel):
    """已经下载到本机的更新包；重启程序后依然在。"""

    file_name: str
    version: str
    size: int
    path: str


class UpdateInstallRead(BaseModel):
    """自动替换的进度快照：解压完成后进程就会退出，界面最后看到的是 launching。"""

    model_config = ConfigDict(from_attributes=True)

    version: str
    status: str
    percent: int
    message: str
    error: str | None = None
    updated_at: float


class UpdateInstallResultRead(BaseModel):
    """上一次自动替换的结果（由退出后的 PowerShell 脚本写入）。"""

    status: str
    message: str
    version: str
    updated_at: float
    log_path: str


class UpdateStateRead(BaseModel):
    current_version: str
    # 打包版才谈得上「替换程序目录」；开发模式只展示版本与提示
    packaged: bool
    latest: UpdateReleaseRead | None = None
    # 已经按 ignored_version 过滤过的结论：界面直接用它决定要不要提示
    has_update: bool = False
    ignored_version: str = ""
    checked_at: float | None = None
    error: str | None = None
    auto_check: bool = True
    download: UpdateProgressRead | None = None
    package: UpdatePackageRead | None = None
    # 能否在当前程序目录里自动替换；不能时 install_reason 说明怎么办
    install_supported: bool = False
    install_reason: str | None = None
    install: UpdateInstallRead | None = None
    last_install: UpdateInstallResultRead | None = None
