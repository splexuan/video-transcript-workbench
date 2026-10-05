import type { DocumentTitle, Job } from '../types'

/**
 * 任务在界面上的名字。
 *
 * 优先级：已识别出的文案标题 → 任务自带的 display_name → 按来源兜底。
 * display_name 是任务 ID 的短前缀（如 `a3f9c2b1`）：链接和整段分享文案又长又乱，
 * 从内容推导还会重名，短 ID 唯一、稳定，能和日志与批次详情对上。本地文件不带
 * display_name，走下面的文件名分支——路径里的随机前缀换成 ID 反而更不可读。
 */
export function jobDisplayTitle(job: Job, titles: DocumentTitle[]) {
  const document = job.document_id
    ? titles.find((item) => item.id === job.document_id)
    : undefined
  if (document) return document.title

  const named = job.display_name?.trim()
  if (named) return named

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
