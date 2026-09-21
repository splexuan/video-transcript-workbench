import type {
  AppSettings,
  Connector,
  CredentialStatus,
  DocumentDetail,
  DocumentSummary,
  DocumentTitle,
  InstallProgress,
  Job,
  JobBatch,
  JobBatchDetail,
  JobBatchPreflight,
  LoginStatus,
  ModelCatalog,
  Page,
  RecognitionModel,
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
  const response = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(body?.detail ?? `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; name: string; version: string }>('/api/health'),
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
    const response = await fetch('/api/jobs/upload', { method: 'POST', body: form })
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null
      throw new Error(body?.detail ?? `导入失败（${response.status}）`)
    }
    return response.json() as Promise<Job>
  },
  cancelJob: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  /** 按原参数重新排队一次（失败/取消后的重试）。 */
  retryJob: (id: string) => request<Job>(`/api/jobs/${id}/retry`, { method: 'POST' }),
  /** 删除终态任务记录；进行中的任务要先取消。 */
  deleteJob: (id: string) =>
    request<{ id: string; removed: boolean }>(`/api/jobs/${id}`, { method: 'DELETE' }),
  documents: (params: { q?: string; platform?: string; limit?: number; cursor?: string | null } = {}) =>
    request<Page<DocumentSummary>>(`/api/documents${queryString(params)}`),
  /** 只回 id 与标题：任务行、批次行要显示文案标题，不必把整个文案库拉回来。 */
  documentTitles: (ids: string[]) =>
    request<DocumentTitle[]>(`/api/documents/titles${queryString({ ids })}`),
  document: (id: string) => request<DocumentDetail>(`/api/documents/${id}`),
  saveDocument: (id: string, payload: Partial<Pick<DocumentSummary, 'title' | 'status'>>) =>
    request<DocumentSummary>(`/api/documents/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  saveSegments: (id: string, segments: DocumentDetail['segments']) =>
    request<DocumentDetail>(`/api/documents/${id}/segments`, {
      method: 'PUT',
      body: JSON.stringify({
        segments: segments.map((segment) => ({
          id: segment.id,
          position: segment.position,
          start_ms: segment.start_ms,
          end_ms: segment.end_ms,
          text: segment.text,
        })),
      }),
    }),
  deleteDocument: (id: string) =>
    request<{ id: string; removed: boolean }>(`/api/documents/${id}`, { method: 'DELETE' }),
  /** 智能分句：把手写/识别文本重排成句级分段，便于逐句校对。 */
  autoFormat: (id: string) =>
    request<DocumentDetail>(`/api/documents/${id}/auto-format`, { method: 'POST' }),
  exportText: async (id: string) => {
    const response = await fetch(api.exportUrl(id, 'txt'))
    if (!response.ok) throw new Error(`读取全文失败（${response.status}）`)
    return response.text()
  },
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
