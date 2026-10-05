import { FileText, RotateCcw } from 'lucide-react'
import { Link } from 'react-router-dom'

import { PlatformBadge } from './PlatformBadge'
import { ProgressBar } from './ProgressBar'
import { StatusChip } from './StatusChip'
import { Thumb } from './Thumb'
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
        {/* 与两个列表共用同一个封面组件：封面被清理或临时地址过期时，
            同样退回图标占位，而不是留一块空洞 */}
        <Thumb
          className="task-exec-cover"
          size={22}
          src={job.document_id ? api.coverUrl(job.document_id) : null}
        />
        <div className="task-exec-info">
          <strong title={title}>{title}</strong>
          <div className="task-exec-meta">
            <PlatformBadge platform={job.platform} />
            <StatusChip status={job.status}>{jobStatusLabels[job.status] ?? job.status}</StatusChip>
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
                    {/* 刚起步时进度不到 1%，留一小截表示「已经在动」 */}
                    <ProgressBar value={job.progress} minPercent={2} label={`“${title}”的进度`} />
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
      {job.batch_id && job.status !== 'completed' && job.status !== 'queued' && job.status !== 'running' && (
        <Link className="text-button task-exec-open" to={`/jobs?batch=${job.batch_id}`}>
          查看所属批次
        </Link>
      )}
      {!job.batch_id && job.status !== 'completed' && job.status !== 'queued' && job.status !== 'running' && (
        <button className="text-button task-exec-open" type="button" onClick={() => onRetry(job.id)}>
          <RotateCcw size={15} /> 重新提取
        </button>
      )}
    </div>
  )
}
