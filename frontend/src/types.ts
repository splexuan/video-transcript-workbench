/**
 * 列表接口的分页响应。
 *
 * `next_cursor` 为 null 表示已经到底；前端不要自己拼游标，把它原样回传即可。
 */
export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface Job {
  id: string
  document_id: string | null
  batch_id: string | null
  batch_position: number | null
  display_name: string | null
  platform: string
  source_type: 'url' | 'file'
  source_value: string
  mode: 'auto' | 'fast' | 'accurate'
  /** 创建任务时在首页指定的模型；为空表示按 mode 走默认路由。 */
  requested_model_id: string | null
  model_id: string | null
  model_name: string | null
  transcript_source: 'subtitle' | 'asr' | null
  status: JobStatus
  stage: string
  progress: number
  message: string
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export type JobBatchStatus = 'queued' | 'running' | 'paused' | 'completed' | 'partial_failed' | 'failed' | 'cancelled'

export interface JobBatchPreflightItem {
  position: number
  raw_source: string
  normalized_source: string | null
  platform: string
  status: 'valid' | 'duplicate' | 'unsupported'
  message: string
}

export interface JobBatchPreflight {
  total_count: number
  valid_count: number
  duplicate_count: number
  unsupported_count: number
  can_submit: boolean
  items: JobBatchPreflightItem[]
}

export interface JobBatch {
  id: string
  title: string
  kind: string
  control_status: 'active' | 'paused' | 'cancelled'
  status: JobBatchStatus
  progress: number
  total_count: number
  queued_count: number
  running_count: number
  completed_count: number
  failed_count: number
  cancelled_count: number
  parent_batch_id: string | null
  created_at: string
  updated_at: string
}

export interface JobBatchDetail extends JobBatch {
  jobs: Job[]
}

export interface DocumentSummary {
  id: string
  title: string
  platform: string
  source_type: string
  source_value: string
  /** 单条提取还是批量提取来的；文案库用它标注来源。 */
  source_kind: 'single' | 'batch'
  /** processing = 提取中，draft = 提取完成；历史数据里可能残留已废弃的 reviewed / exported。 */
  status: 'processing' | 'draft'
  duration_seconds: number | null
  word_count: number
  /** 来源作品作者；本地文件没有。 */
  uploader: string | null
  /** 是否存了封面图，图片由 /api/documents/{id}/cover 提供。 */
  has_cover: boolean
  created_at: string
  updated_at: string
}

/** 只含 id 与标题的轻量文档表示；任务行、批次行按它反查标题。 */
export interface DocumentTitle {
  id: string
  title: string
}

/** 文案库里的作者聚合：一位作者一条。 */
export interface AuthorSummary {
  name: string
  count: number
  total_words: number
  /** 第一次与最近一次提取这位作者的作品。 */
  first_at: string
  latest_at: string
}

/** 作者列表：封顶返回 + 总数，界面据此说明「只显示了前 N 位」。 */
export interface AuthorPage {
  items: AuthorSummary[]
  total: number
}

export interface TranscriptSegment {
  id: number
  position: number
  start_ms: number | null
  end_ms: number | null
  raw_text: string
  text: string
}

export interface DocumentDetail extends DocumentSummary {
  segments: TranscriptSegment[]
  /** 来源作品介绍，通常比标题长；没有就是 null。 */
  description: string | null
  /**
   * 正文形态：false = 一行一句，true = 「智能分句」整理过的自然段（段落之间空行）。
   * 一条分段就是一块正文，这个标记只决定段落之间是否空行。
   */
  paragraph_body: boolean
  /** 原始音轨是否还留在本机；false 时编辑页不渲染播放器。 */
  media_available: boolean
}

/** 智能分句的结果：整理后的分段已经落库，document 是可直接替换的新文案。 */
export interface PolishedText {
  text: string
  /** 整理后的自然段数量与句子数量，用于界面上的说明文字。 */
  paragraphs: number
  sentences: number
  /** 合并掉的滚动重复条目数；0 表示没有发现重复。 */
  merged: number
  /** 是否调用了标点模型；正文本来就带标点时为 false。 */
  punctuated: boolean
  document: DocumentDetail
}

/**
 * 进入编辑页时带上的位置信息。
 *
 * 编辑页可能从工作台、文案库或任务队列打开，返回按钮要回到真正来的地方，
 * 而不是一律回到文案库。直接打开链接时没有这份信息，退回文案库。
 */
export interface EditorNavState {
  from?: string
  label?: string
}

export interface Connector {
  id: string
  name: string
  status: 'ready' | 'needs_setup' | 'planned'
  detail: string
}

/** 平台登录 Cookie 的配置状态；内容本身不会回传到前端。 */
export interface CredentialStatus {
  platform: string
  configured: boolean
  /** false 表示凭据可选：不配置也能用，配置后支持会员或登录可见内容（如 B站）。 */
  required: boolean
  entries: number
  updated_at: number | null
}

/**
 * 浏览器助手会话状态。
 * guest：打开浏览器取游客身份，用户无需操作；login：等待用户扫码登录。
 */
export interface LoginStatus {
  platform: string
  mode: 'guest' | 'login'
  status: 'idle' | 'running' | 'saved' | 'failed' | 'cancelled'
  message: string
  entries: number
}

export interface AppSettings {
  theme: 'system' | 'light' | 'dark'
  /** 新建任务时默认选中的识别模型 id。 */
  default_model: string
  /** 是否优先使用平台字幕；关掉后始终用所选模型重新转写。 */
  prefer_subtitle: boolean
  keep_media: boolean
  storage_path: string
  /** 兜底解析接口的 API Key 是否已配置；明文只写不读，后端不会回传。 */
  fallback_api_key_set: boolean
  /** 仅提交时使用：写入兜底解析的 API Key（空串表示清除）；读取设置时不会返回。 */
  fallback_api_key?: string
  /** 「语音合成」默认使用的模型 id；留空表示跟随引擎默认。 */
  tts_model: string
  /** 默认音色（ZipVoice 参考音频的 id）；null 表示用引擎默认。 */
  tts_voice: string | null
}

/* ---------- 语音合成 ---------- */

/**
 * 一个音色。ZipVoice 是零样本克隆，音色来自一段参考音频，id 即这段音频的标识——
 * 内置音色随模型发布，自定义音色由用户上传后出现在这里。
 */
export interface TtsVoice {
  id: string
  name: string
  /** 这段参考音频里逐字说出的内容，必须与音频一致，否则克隆出的音色会明显变差。 */
  reference_text: string
  /** 内置音色不可删除。 */
  builtin: boolean
  /** 参考音频时长（秒）：画波形、判断选区是否过短都要它；内置音色为 0。 */
  seconds: number
  /** 自定义音色的参考音频地址（试听与波形编辑都用它）；内置音色为 null。 */
  audio_url: string | null
}

/**
 * 参考音频改动后的回执。
 *
 * `text_source` 说明参考文本是怎么来的：手填 / 转写 / 沿用旧值。最后一种发生在
 * 转写没成功时——界面要如实说明（`note`），别让用户以为文字已经换成新的了。
 */
export interface TtsVoiceEditResult {
  voice: TtsVoice
  text_source: 'typed' | 'transcribed' | 'kept'
  note: string
}

/** 波形编辑器里的一段选区（秒）；`start === 0 && end === 0` 表示还没加载音频。 */
export interface AudioSelection {
  start: number
  end: number
}

/** 「音色克隆」里自动转写的结果：文字给用户核对，`truncated` 表示参考音频只取了开头一段。 */
export interface TtsTranscribeResult {
  text: string
  seconds: number
  truncated: boolean
}

export interface TtsModelOption {
  id: string
  name: string
  state: ModelState
  ready: boolean
  approx_bytes: number
}

export interface TtsConfig {
  ready: boolean
  message: string
  model: TtsModelOption | null
  models: TtsModelOption[]
  voices: TtsVoice[]
  voice: string
  speed: number
  max_chars: number
  /** 估算用：每秒音频约几个字（实测中文）。 */
  chars_per_second: number
  /** 估算用：单实例合成耗时与音频时长之比（实测 fp32 版、8 步采样 CPU ≈ 1.3）。 */
  realtime_factor: number
  /** 这台机器会把句子摊到几个实例上并行合成（核数少时为 1）。 */
  parallel_lanes: number
}

export type TtsJobStatus = 'pending' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled'

export interface TtsJob {
  id: string
  status: TtsJobStatus
  message: string
  percent: number
  model_id: string
  voice: string
  speed: number
  /** 提交时的原文：点记录能填回输入框。早期记录没存，是空串。 */
  text: string
  chars: number
  sentences_done: number
  sentences_total: number
  /** 生成音频的时长（秒）与本次合成耗时（秒）。 */
  duration: number
  elapsed: number
  error: string | null
  /** 完成后才有：`/api/tts/jobs/{id}/audio`。 */
  audio_url: string | null
  created_at: number
  updated_at: number
}

/** 后端 /api/health 的返回；界面用它显示版本号。 */
export interface HealthInfo {
  status: string
  name: string
  version: string
}

export type ModelState = 'ready' | 'missing' | 'partial' | 'broken' | 'installing'

export interface InstallProgress {
  model_id: string
  action: 'download'
  status: 'pending' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled'
  percent: number
  downloaded_bytes: number
  total_bytes: number
  file_name: string
  completed_files: number
  total_files: number
  message: string
  error: string | null
  updated_at: number
}

export interface RecognitionModel {
  id: string
  name: string
  /**
   * punctuation 是辅助模型（标点恢复），不参与语音识别，只服务「智能分句」；
   * tts 是把文案读成音频的语音合成模型。
   */
  engine: 'sensevoice' | 'faster_whisper' | 'punctuation' | 'tts'
  tier: 'fast' | 'accurate' | 'utility'
  description: string
  languages: string
  recommended: boolean
  note: string | null
  state: ModelState
  path: string | null
  installed_bytes: number
  approx_bytes: number
  missing_files: string[]
  message: string
  source: string | null
  engine_ready: boolean
  progress: InstallProgress | null
}

export interface EngineStatus {
  engine: 'sensevoice' | 'faster_whisper' | 'punctuation' | 'tts'
  package_ready: boolean
  model_ready: boolean
  ready: boolean
  active_model: string | null
  specs: string[]
}

export interface ModelCatalog {
  storage: {
    models_root: string
    total_bytes: number
    disk_free_bytes: number | null
  }
  engines: EngineStatus[]
  models: RecognitionModel[]
  active_tasks: InstallProgress[]
}
