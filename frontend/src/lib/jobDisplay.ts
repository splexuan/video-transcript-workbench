import type { DocumentSummary, Job } from '../types'

export function jobDisplayTitle(job: Job, documents: DocumentSummary[]) {
  const document = job.document_id
    ? documents.find((item) => item.id === job.document_id)
    : undefined
  if (document) return document.title

  if (job.source_type === 'file') {
    return job.source_value.replaceAll('\\', '/').split('/').pop() || '本地文件'
  }

  try {
    const host = new URL(job.source_value).hostname.replace(/^www\./, '')
    return host.includes('bilibili') || host === 'b23.tv' ? 'B站视频' : host
  } catch {
    return job.source_value
  }
}
