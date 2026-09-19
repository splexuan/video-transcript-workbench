import { Film, FileText, RotateCcw } from 'lucide-react'

import { PlatformBadge } from './PlatformBadge'
import { api } from '../lib/api'
import { jobStatusLabels } from '../lib/labels'
import type { Job } from '../types'

/**
 * 「任务执行」的流程步骤。后端阶段归并成四步展示：
 * fetching_subtitle（B站字幕任务）与 downloading 都算「获取内容」。
 */
const flowSteps: { keys: string[]; label: string }[] = [
  { keys: ['resolving'], label: '读取来源' },
  { keys: ['fetching_subtitle', 'downloading'], label: '获取内容' },
  { keys: ['transcribing'], label: '识别语音' },
  { keys: ['writing', 'done'], label: '整理文案' },
]

type StepState = 'done' | 'current' | 'pending'

/**
 * 右栏的「任务执行」卡片：封面、标题，以及当前任务每一步的流程和进度。
 */
export function TaskExecution({
  job,
  title,
  onOpenDocument,
  onRetry,
}: {
  job: Job
  title: string
  onOpenDocument: (documentId: string) => void
  onRetry: (jobId: string) => void
}) {
  const stageIndex = flowSteps.findIndex((step) => step.keys.includes(job.stage))

  function stepState(index: number): StepState {
    if (job.status === 'completed') return 'done'
    // waiting/queued 或未知阶段：还没踏进第一步
    if (stageIndex === -1) return index === 0 ? 'current' : 'pending'
    return index < stageIndex ? 'done' : index === stageIndex ? 'current' : 'pending'
  }

  return (
    <div className={`task-exec${job.status === 'failed' ? ' failed' : ''}`}>
      <div className="task-exec-head">
        {job.document_id ? (
          <img
            className="task-exec-cover"
            src={api.coverUrl(job.document_id)}
            alt=""
            onError={(event) => { event.currentTarget.style.visibility = 'hidden' }}
          />
        ) : (
          <span className="task-exec-cover task-exec-placeholder" aria-hidden="true">
            <Film size={22} />
          </span>
        )}
        <div className="task-exec-info">
          <strong title={title}>{title}</strong>
          <div className="task-exec-meta">
            <PlatformBadge platform={job.platform} />
            <span className={`status-chip ${job.status}`}>{jobStatusLabels[job.status] ?? job.status}</span>
          </div>
        </div>
      </div>

      {job.status === 'failed' && (
        <p className="task-exec-error" role="alert">{job.error_message ?? job.message}</p>
      )}

      <ol className="task-flow">
        {flowSteps.map((step, index) => {
          const state = stepState(index)
          return (
            <li key={step.label} className={`task-flow-step ${state}`}>
              <span className="task-flow-dot" aria-hidden="true" />
              <div className="task-flow-body">
                <strong>{step.label}</strong>
                {state === 'current' && job.status === 'running' && (
                  <>
                    <small>{job.message}</small>
                    <div className="progress-track" aria-label={`进度 ${job.progress}%`}>
                      <span style={{ width: `${Math.max(job.progress, 2)}%` }} />
                    </div>
                  </>
                )}
                {state === 'current' && job.status === 'queued' && <small>排队等待中…</small>}
                {state === 'current' && job.status === 'failed' && <small>{job.message}</small>}
              </div>
            </li>
          )
        })}
      </ol>

      {job.status === 'completed' && job.document_id && (
        <button className="text-button task-exec-open" type="button" onClick={() => onOpenDocument(job.document_id as string)}>
          <FileText size={15} /> 查看文案
        </button>
      )}
      {job.status !== 'completed' && job.status !== 'queued' && job.status !== 'running' && (
        <button className="text-button task-exec-open" type="button" onClick={() => onRetry(job.id)}>
          <RotateCcw size={15} /> 重新提取
        </button>
      )}
    </div>
  )
}
