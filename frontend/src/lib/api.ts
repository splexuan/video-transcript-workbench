import type {
  AppSettings,
  Connector,
  CredentialStatus,
  DocumentDetail,
  DocumentSummary,
  InstallProgress,
  Job,
  LoginStatus,
  ModelCatalog,
  RecognitionModel,
} from '../types'

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
  jobs: (limit = 50) => request<Job[]>(`/api/jobs?limit=${limit}`),
  createJob: (payload: { source_type: 'url' | 'file'; source: string; mode: string; model_id?: string | null; prefer_subtitle?: boolean }) =>
    request<Job>('/api/jobs', { method: 'POST', body: JSON.stringify(payload) }),
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
  documents: (query = '') =>
    request<DocumentSummary[]>(`/api/documents${query ? `?q=${encodeURIComponent(query)}` : ''}`),
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
