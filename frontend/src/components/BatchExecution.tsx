import { AlertTriangle, FileText } from 'lucide-react'

import { PlatformBadge } from './PlatformBadge'
import { batchStatusLabels, isOpenBatch, isPossiblyStalled, jobDetailText, normalizedProgress, updatedAtFormatter } from '../lib/jobBatch'
import { jobDisplayTitle } from '../lib/jobDisplay'
import { jobStatusLabels, stageLabels } from '../lib/labels'
import type { DocumentTitle, JobBatchDetail } from '../types'

/**
 * 右栏「任务执行」的批量形态：一个批次，加上它下面**每一条**子任务的实时详情。
 *
 * 数据与任务队列页的批次卡片同源（同一个 `JobBatchDetail`），差别只在排版：
 * 这里是 300～380px 的窄栏，所以每条子任务竖向堆叠——状态芯片收进标题行、
 * 平台徽标与阶段/时间并成一行，宽度不够时也不会把行挤破。
 */
export function BatchExecution({
  batch,
  titles,
  onOpenDocument,
}: {
  batch: JobBatchDetail
  /** 已识别出的文案标题，用来把子任务行的名字从任务 ID 换成真正的标题。 */
  titles: DocumentTitle[]
  onOpenDocument: (documentId: string) => void
}) {
  return (
    <div className="task-batch">
      <div className="task-batch-head">
        <strong title={batch.title}>{batch.title}</strong>
        <span className={`status-chip ${batch.status}`}>
          {batchStatusLabels[batch.status] ?? batch.status}
        </span>
      </div>
      <p className="task-batch-summary">
        共 {batch.total_count} 条 · 已完成 {batch.completed_count}
        {batch.running_count > 0 && ` · 处理中 ${batch.running_count}`}
        {batch.failed_count > 0 && ` · 失败 ${batch.failed_count}`}
        {batch.cancelled_count > 0 && ` · 取消 ${batch.cancelled_count}`}
      </p>
      {/* 与任务队列页同一个口径：批次结束后不再留一条停在 100% 的进度条 */}
      {isOpenBatch(batch.status) && (
        <div className="task-batch-progress">
          <div
            className="progress-track"
            role="progressbar"
            aria-label="批次进度"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={batch.progress}
          >
            <span style={{ width: `${batch.progress}%` }} />
          </div>
          <span>{batch.progress}%</span>
        </div>
      )}

      <ol className="task-batch-list">
        {batch.jobs.map((job) => {
          const title = jobDisplayTitle(job, titles)
          const detail = jobDetailText(job)
          const progress = normalizedProgress(job)
          const stalled = isPossiblyStalled(job)
          return (
            <li className={`task-batch-item ${job.status}`} key={job.id}>
              <span className={`job-state ${job.status}`} aria-hidden="true" />
              <div className="task-batch-body">
                <div className="task-batch-title-row">
                  <span className="task-batch-index">#{job.batch_position}</span>
                  <span className="task-batch-title" title={job.source_value}>{title}</span>
                  <span className={`status-chip ${job.status}`}>
                    {jobStatusLabels[job.status] ?? job.status}
                  </span>
                </div>
                <div className="task-batch-meta">
                  <PlatformBadge platform={job.platform} />
                  <span className="task-batch-stage">{stageLabels[job.stage] ?? job.stage}</span>
                  <time dateTime={job.updated_at}>
                    更新于 {updatedAtFormatter.format(new Date(job.updated_at))}
                  </time>
                </div>
                {job.status !== 'completed' && detail && (
                  <p className={`task-batch-message${job.status === 'failed' ? ' error' : ''}`} title={detail}>
                    {detail}
                  </p>
                )}
                {job.status === 'running' && (
                  <div className="task-batch-bar">
                    <div
                      className="progress-track"
                      role="progressbar"
                      aria-label={`第 ${job.batch_position} 条进度`}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={progress}
                    >
                      <span style={{ width: `${progress}%` }} />
                    </div>
                    <span>{progress}%</span>
                  </div>
                )}
                {stalled && (
                  <p className="task-batch-stalled" role="status">
                    <AlertTriangle size={13} aria-hidden="true" />
                    超过 5 分钟没有新进度，可能仍在处理耗时步骤
                  </p>
                )}
                {job.status === 'completed' && job.document_id && (
                  <button
                    className="text-button task-batch-open"
                    type="button"
                    onClick={() => onOpenDocument(job.document_id as string)}
                  >
                    <FileText size={14} /> 查看文案
                  </button>
                )}
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
