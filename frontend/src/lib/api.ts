import type {
  AppSettings,
  AudioSelection,
  AuthorPage,
  Connector,
  CredentialStatus,
  DocumentDetail,
  DocumentSummary,
  DocumentTitle,
  HealthInfo,
  InstallProgress,
  Job,
  JobBatch,
  JobBatchDetail,
  JobBatchPreflight,
  LoginStatus,
  ModelCatalog,
  Page,
  PolishedText,
  RecognitionModel,
  TtsConfig,
  TtsJob,
  TtsTranscribeResult,
  TtsVoice,
  TtsVoiceEditResult,
} from '../types'

/** 拼查询串；空值跳过，数组展开成重复参数（后端的 ids 用重复参数收）。 */
function queryString(params: Record<string, string | number | boolean | null | undefined | string[]>) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, item)
      continue
    }
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  // FormData 必须原样交给 fetch：手动设 Content-Type 会丢掉 boundary 参数，
  // 而 multipart 请求体一旦被标成 application/json，后端就解析不出文件字段（422）。
  // 项目里唯一在用的上传接口（uploadJob）一直是直接调 fetch 绕开这里的，
  // 现在把这件事收进 request，调用方不必再记这条规矩。
  const body = init?.body
  const isFormData = typeof FormData !== 'undefined' && body instanceof FormData
  const response = await fetch(url, {
    ...init,
    ...(isFormData ? {} : { headers: { 'Content-Type': 'application/json', ...init?.headers } }),
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(body?.detail ?? `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<HealthInfo>('/api/health'),
  jobs: (params: { standalone?: boolean; status?: string[]; limit?: number; cursor?: string | null } = {}) =>
    request<Page<Job>>(`/api/jobs${queryString(params)}`),
  createJob: (payload: { source_type: 'url' | 'file'; source: string; mode: string; model_id?: string | null; prefer_subtitle?: boolean }) =>
    request<Job>('/api/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  preflightBatch: (sources: string[]) =>
    request<JobBatchPreflight>('/api/job-batches/preflight', {
      method: 'POST',
      body: JSON.stringify({ sources }),
    }),
  createBatch: (payload: {
    title?: string
    sources: string[]
    mode: string
    model_id?: string | null
    prefer_subtitle?: boolean
    client_request_id: string
  }) => request<JobBatchDetail>('/api/job-batches', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  /** 批次的 status 是派生值（含 paused / partial_failed），取值集合与任务不同。 */
  batches: (params: { status?: string[]; limit?: number; cursor?: string | null } = {}) =>
    request<Page<JobBatch>>(`/api/job-batches${queryString(params)}`),
  batch: (id: string) => request<JobBatchDetail>(`/api/job-batches/${id}`),
  pauseBatch: (id: string) =>
    request<JobBatchDetail>(`/api/job-batches/${id}/pause`, { method: 'POST' }),
  resumeBatch: (id: string) =>
    request<JobBatchDetail>(`/api/job-batches/${id}/resume`, { method: 'POST' }),
  cancelBatch: (id: string) =>
    request<JobBatchDetail>(`/api/job-batches/${id}/cancel`, { method: 'POST' }),
  retryFailedBatch: (id: string) =>
    request<JobBatchDetail>(`/api/job-batches/${id}/retry-failed`, { method: 'POST' }),
  uploadJob: async (file: File, mode: string, modelId?: string | null, preferSubtitle = false) => {
    const form = new FormData()
    form.append('file', file)
    form.append('mode', mode)
    if (modelId) form.append('model_id', modelId)
    form.append('prefer_subtitle', String(preferSubtitle))
    // 这里仍直接调 fetch，而不是 request：上传失败要给出「导入失败」而不是通用的
    // 「请求失败」，前者更贴近用户理解。request 现在也已正确支持 FormData。
    const response = await fetch('/api/jobs/upload', { method: 'POST', body: form })
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null
      throw new Error(body?.detail ?? `导入失败（${response.status}）`)
    }
    return response.json() as Promise<Job>
  },
  /** 多文件作为同一个批次导入：后端一次收齐文件后建批次，右栏按批次显示进度。 */
  uploadJobBatch: (files: File[], mode: string, modelId?: string | null, preferSubtitle = true) => {
    const form = new FormData()
    for (const file of files) form.append('files', file)
    form.append('mode', mode)
    if (modelId) form.append('model_id', modelId)
    form.append('prefer_subtitle', String(preferSubtitle))
    return request<JobBatchDetail>('/api/job-batches/upload', { method: 'POST', body: form })
  },
  cancelJob: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  /** 按原参数重新排队一次（失败/取消后的重试）。 */
  retryJob: (id: string) => request<Job>(`/api/jobs/${id}/retry`, { method: 'POST' }),
  /** sort 只认白名单：updated / created / words；传别的会被后端拒绝。 */
  documents: (params: { q?: string; platform?: string; uploader?: string; sort?: string; limit?: number; cursor?: string | null } = {}) =>
    request<Page<DocumentSummary>>(`/api/documents${queryString(params)}`),
  /** 按作者浏览文案库：每位作者一条聚合（封顶返回，q 可按名字找）。 */
  documentAuthors: (params: { q?: string; limit?: number } = {}) =>
    request<AuthorPage>(`/api/documents/authors${queryString(params)}`),
  /**
   * 批量导出：打包成 zip，由浏览器直接下载（和单篇导出一样走 `<a download>`）。
   * 给了 ids 就只导这几篇（其余筛选项会被忽略），没给就导当前筛选下的全部。
   */
  exportDocumentsUrl: (params: { ids?: string[]; q?: string; platform?: string; uploader?: string; sort?: string; format?: 'txt' | 'srt' | 'vtt' | 'json' }) =>
    `/api/documents/export${queryString(params)}`,
  /** 只回 id 与标题：任务行、批次行要显示文案标题，不必把整个文案库拉回来。 */
  documentTitles: (ids: string[]) =>
    request<DocumentTitle[]>(`/api/documents/titles${queryString({ ids })}`),
  document: (id: string) => request<DocumentDetail>(`/api/documents/${id}`),
  saveDocument: (id: string, payload: Partial<Pick<DocumentSummary, 'title'>>) =>
    request<DocumentSummary>(`/api/documents/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  /**
   * 整篇正文保存：一行一条分段，空行表示段落之间空一行。
   * 行数没变就地改字（时间码不动），行数变了按行重建（时间按字数比例分摊）。
   */
  saveBody: (id: string, text: string) =>
    request<DocumentDetail>(`/api/documents/${id}/body`, {
      method: 'PUT',
      body: JSON.stringify({ text }),
    }),
  deleteDocument: (id: string) =>
    request<{ id: string; removed: boolean }>(`/api/documents/${id}`, { method: 'DELETE' }),
  /** 智能分句：整理成带标点的自然段并直接落库，返回整理后的文案。 */
  polish: (id: string) =>
    request<PolishedText>(`/api/documents/${id}/polish`, { method: 'POST' }),
  /** 恢复原始分段：用识别原文重建「一行一句」的分段。 */
  restoreLines: (id: string) =>
    request<DocumentDetail>(`/api/documents/${id}/restore-lines`, { method: 'POST' }),
  exportUrl: (id: string, format: 'txt' | 'srt' | 'vtt' | 'json') =>
    `/api/documents/${id}/export?format=${format}`,
  /** 原始音轨的播放地址；未保留时该地址返回 404。 */
  mediaUrl: (id: string) => `/api/documents/${id}/media`,
  /** 来源作品封面图；没有封面时该地址返回 404。 */
  coverUrl: (id: string) => `/api/documents/${id}/cover`,
  connectors: () => request<Connector[]>('/api/connectors'),
  credentials: () => request<CredentialStatus[]>('/api/credentials'),
  saveCredential: (platform: string, cookie: string) =>
    request<CredentialStatus>(`/api/credentials/${platform}`, {
      method: 'PUT',
      body: JSON.stringify({ cookie }),
    }),
  deleteCredential: (platform: string) =>
    request<{ platform: string; removed: boolean }>(`/api/credentials/${platform}`, {
      method: 'DELETE',
    }),
  /** 启动浏览器助手：guest 取游客身份（无需操作），login 等待扫码登录。 */
  startBrowserLogin: (platform: string, mode: 'guest' | 'login' = 'guest') =>
    request<LoginStatus>(`/api/credentials/${platform}/login?mode=${mode}`, { method: 'POST' }),
  browserLoginStatus: (platform: string) =>
    request<LoginStatus>(`/api/credentials/${platform}/login`),
  cancelBrowserLogin: (platform: string) =>
    request<LoginStatus>(`/api/credentials/${platform}/login`, { method: 'DELETE' }),
  settings: () => request<AppSettings>('/api/settings'),
  updateSettings: (payload: Partial<AppSettings>) =>
    request<AppSettings>('/api/settings', { method: 'PATCH', body: JSON.stringify(payload) }),
  /** 语音合成页的配置：可选模型与音色、当前选择、估算依据。 */
  ttsConfig: () => request<TtsConfig>('/api/tts'),
  ttsVoices: () => request<TtsVoice[]>('/api/tts/voices'),
  /**
   * 转写一段参考音频：用来自动填「音色克隆」里必须逐字一致的参考文本。
   * 带选区时只转写选中那一段——文字与最终保存的音频保持同一段，裁剪后不必再删字。
   */
  transcribeTtsVoice: (audio: File, selection?: AudioSelection) => {
    const form = new FormData()
    form.append('audio', audio)
    if (selection) {
      form.append('start', String(selection.start))
      form.append('end', String(selection.end))
    }
    return request<TtsTranscribeResult>('/api/tts/voices/transcribe', { method: 'POST', body: form })
  },
  /** 上传一段参考音频存成自己的音色（multipart：音频文件 + 名称 + 参考文本 + 可选选区）。 */
  createTtsVoice: (payload: {
    name: string
    reference_text: string
    audio: File
    selection?: AudioSelection
  }) => {
    const form = new FormData()
    form.append('name', payload.name)
    form.append('reference_text', payload.reference_text)
    form.append('audio', payload.audio)
    if (payload.selection) {
      form.append('start', String(payload.selection.start))
      form.append('end', String(payload.selection.end))
    }
    return request<TtsVoice>('/api/tts/voices', { method: 'POST', body: form })
  },
  /** 自定义音色的参考音频地址：试听与画波形都用它。 */
  ttsVoiceAudioUrl: (id: string) => `/api/tts/voices/${id}/audio`,
  /** 就地换掉参考音频（不新建音色）。文本按 手填 → 转写 → 沿用旧值 的顺序定。 */
  replaceTtsVoiceAudio: (payload: {
    id: string
    audio: File
    name?: string
    reference_text?: string
    selection?: AudioSelection
  }) => {
    const form = new FormData()
    form.append('audio', payload.audio)
    if (payload.name) form.append('name', payload.name)
    if (payload.reference_text) form.append('reference_text', payload.reference_text)
    if (payload.selection) {
      form.append('start', String(payload.selection.start))
      form.append('end', String(payload.selection.end))
    }
    return request<TtsVoiceEditResult>(`/api/tts/voices/${payload.id}/audio`, { method: 'POST', body: form })
  },
  /** 把参考音频裁到选区；`retranscribe` 为真时按裁剪后的音频重新转写参考文本。 */
  trimTtsVoice: (payload: {
    id: string
    selection: AudioSelection
    reference_text?: string
    retranscribe?: boolean
  }) =>
    request<TtsVoiceEditResult>(`/api/tts/voices/${payload.id}/trim`, {
      method: 'POST',
      body: JSON.stringify({
        start: payload.selection.start,
        end: payload.selection.end,
        reference_text: payload.reference_text,
        retranscribe: payload.retranscribe ?? false,
      }),
    }),
  /** 改音色名（只动元数据，不搬音频）。 */
  renameTtsVoice: (id: string, name: string) =>
    request<TtsVoiceEditResult>(`/api/tts/voices/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    }),
  /** 按当前参考音频重新转写一遍参考文本（会直接写盘）。 */
  retranscribeTtsVoice: (id: string) =>
    request<TtsVoiceEditResult>(`/api/tts/voices/${id}/retranscribe`, { method: 'POST' }),
  /**
   * 按选区转写已保存的参考音频，**不写盘**：只回填编辑框，点「保存修改」才落盘。
   * 编辑弹窗用它而不是上面那个写盘接口，否则「看一眼」也会把音色改了。
   */
  previewTtsVoiceTranscript: (id: string, selection?: AudioSelection) =>
    request<TtsTranscribeResult>(`/api/tts/voices/${id}/preview-transcript`, {
      method: 'POST',
      body: JSON.stringify(
        selection ? { start: selection.start, end: selection.end } : { start: null, end: null },
      ),
    }),
  deleteTtsVoice: (id: string) =>
    request<{ id: string; removed: boolean }>(`/api/tts/voices/${id}`, { method: 'DELETE' }),
  /** 提交合成任务（长文本要几分钟，返回后轮询 ttsJob）。 */
  createTtsJob: (payload: { text: string; voice?: string; speed?: number; model_id?: string }) =>
    request<TtsJob>('/api/tts/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  ttsJobs: () => request<TtsJob[]>('/api/tts/jobs'),
  ttsJob: (id: string) => request<TtsJob>(`/api/tts/jobs/${id}`),
  cancelTtsJob: (id: string) => request<TtsJob>(`/api/tts/jobs/${id}/cancel`, { method: 'POST' }),
  /** 清空所有已结束的合成记录与音频；正在合成的那条不动。 */
  clearTtsJobs: () => request<{ removed: number }>('/api/tts/jobs', { method: 'DELETE' }),
  deleteTtsJob: (id: string) =>
    request<{ id: string; removed: boolean }>(`/api/tts/jobs/${id}`, { method: 'DELETE' }),
  /** 音频地址：播放用 inline，下载加 download=1（后端按它决定 Content-Disposition）。 */
  ttsAudioUrl: (id: string, format: 'wav' | 'mp3' = 'wav', download = false) =>
    `/api/tts/jobs/${id}/audio?format=${format}${download ? '&download=1' : ''}`,
  models: () => request<ModelCatalog>('/api/models'),
  downloadModel: (id: string) =>
    request<InstallProgress>(`/api/models/${id}/download`, { method: 'POST' }),
  cancelModelInstall: (id: string) =>
    request<InstallProgress>(`/api/models/${id}/cancel`, { method: 'POST' }),
  verifyModel: (id: string) =>
    request<RecognitionModel>(`/api/models/${id}/verify`, { method: 'POST' }),
  deleteModel: (id: string) =>
    request<{ removed: boolean; model_id: string }>(`/api/models/${id}`, { method: 'DELETE' }),
}
