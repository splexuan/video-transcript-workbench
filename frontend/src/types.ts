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
  status: 'processing' | 'draft' | 'reviewed' | 'exported'
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
  /** 文案来源：subtitle（平台字幕）或 asr（本地识别）；字幕不做段落合并。 */
  transcript_source: 'subtitle' | 'asr' | null
  /** 原始音轨是否还留在本机；false 时编辑页不渲染播放器。 */
  media_available: boolean
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
  engine: 'sensevoice' | 'faster_whisper'
  tier: 'fast' | 'accurate'
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
  engine: 'sensevoice' | 'faster_whisper'
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
