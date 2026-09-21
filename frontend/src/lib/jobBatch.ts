import type { Job, JobBatchStatus } from '../types'

/**
 * 批次的共享口径。
 *
 * 首页右栏与任务队列页都会渲染批次里的子任务，两处必须用同一套状态文案、
 * 同一条停滞判据、同一个标题来源；否则同一条任务在两个页面会显示成两回事。
 */

/** 批次状态文案；取值集合与任务状态（jobStatusLabels）不同，不能混用。 */
export const batchStatusLabels: Record<string, string> = {
  queued: '等待中',
  running: '进行中',
  paused: '已暂停',
  completed: '已完成',
  partial_failed: '部分失败',
  failed: '失败',
  cancelled: '已取消',
}

/** 超过这个时间没有新的 updated_at 就提示「可能仍在处理耗时步骤」。 */
export const stalledAfterMs = 5 * 60 * 1000

/**
 * 正在推进的批次。首页右栏据此决定要不要接着盯一个批次：换页或刷新之后仍然把
 * 在跑的批次显示出来，而早就结束的不再翻出来。
 *
 * 这里**不含 paused**：暂停的批次不会自己结束，如果算进来就会被永久钉在首页，
 * 而首页右栏并没有「继续 / 取消」入口，用户没法把它收掉。
 */
export function isRunningBatch(status: JobBatchStatus) {
  return status === 'queued' || status === 'running'
}

/**
 * 还没结束的批次（含暂停）。任务队列页据此决定显示「暂停」还是「继续」——
 * 暂停属于未结束，仍然可以被继续，所以那边必须把它算作 active。
 */
export function isOpenBatch(status: JobBatchStatus) {
  return status === 'queued' || status === 'running' || status === 'paused'
}

/** 子任务行的「更新于」时间：精确到秒，够判断这条还在不在推进。 */
export const updatedAtFormatter = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

export function isPossiblyStalled(job: Job) {
  if (job.status !== 'running') return false
  const updatedAt = new Date(job.updated_at).getTime()
  return Number.isFinite(updatedAt) && Date.now() - updatedAt > stalledAfterMs
}

/** 进度收敛到 0-100；后端不会越界，这里兜一层，避免进度条画出轨道。 */
export function normalizedProgress(job: Job) {
  return Math.min(100, Math.max(0, job.progress))
}

/** 失败项要说清失败原因；进行中的任务说明正在做什么，两者取值不同。 */
export function jobDetailText(job: Job) {
  return job.status === 'failed' ? job.error_message ?? job.message : job.message
}
