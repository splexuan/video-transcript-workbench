import { AlertTriangle, Ban, ChevronDown, ChevronUp, Cpu, FileText, ListTodo, LoaderCircle, Pause, Play, RotateCcw, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DocumentPreviewModal } from '../components/DocumentPreviewModal'
import { EmptyState } from '../components/EmptyState'
import { PlatformBadge } from '../components/PlatformBadge'
import { api } from '../lib/api'
import { jobDisplayTitle } from '../lib/jobDisplay'
import { jobStatusLabels, shortModelName, stageLabels } from '../lib/labels'
import type { DocumentSummary, Job, JobBatch, JobBatchDetail } from '../types'

const batchStatusLabels: Record<string, string> = {
  queued: '等待中',
  running: '进行中',
  paused: '已暂停',
  completed: '已完成',
  partial_failed: '部分失败',
  failed: '失败',
  cancelled: '已取消',
}

// 说清「为什么用了识别模型」：只有 B站可能带字幕，本地文件与其它平台都只能本地转写。
function sourceHint(job: Job) {
  if (job.transcript_source === 'subtitle') return '直接读取平台字幕，没有调用识别模型'
  if (job.source_type === 'file') return '本地音视频文件，使用识别模型转写'
  if (job.platform === 'bilibili') return '该视频没有可用字幕，已改用本地识别'
  return '该平台没有可读取的字幕，已下载音轨在本机识别'
}

// 只到分钟：秒对判断任务先后没有意义，还会把信息条撑长。
const createdAtFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const updatedAtFormatter = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

const stalledAfterMs = 5 * 60 * 1000

function isPossiblyStalled(job: Job) {
  if (job.status !== 'running') return false
  const updatedAt = new Date(job.updated_at).getTime()
  return Number.isFinite(updatedAt) && Date.now() - updatedAt > stalledAfterMs
}

function normalizedProgress(job: Job) {
  return Math.min(100, Math.max(0, job.progress))
}

export function JobsPage() {
  const [searchParams] = useSearchParams()
  const [jobs, setJobs] = useState<Job[]>([])
  const [batches, setBatches] = useState<JobBatch[]>([])
  const [expandedBatchId, setExpandedBatchId] = useState(searchParams.get('batch') ?? '')
  const [batchDetail, setBatchDetail] = useState<JobBatchDetail | null>(null)
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [error, setError] = useState('')
  // 「打开文案」先弹窗预览，要改再进编辑页
  const [previewId, setPreviewId] = useState('')
  // 正在执行取消/删除/重试的任务：按钮禁用，避免重复点击
  const [busyId, setBusyId] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')

  function load() {
    Promise.all([api.jobs(200), api.documents(), api.batches()])
      .then(([nextJobs, nextDocuments, nextBatches]) => {
        setJobs(nextJobs)
        setDocuments(nextDocuments)
        setBatches(nextBatches)
        setError('')
      })
      .catch((reason: Error) => setError(reason.message))
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 2500)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!expandedBatchId) return
    let active = true
    const loadDetail = () => api.batch(expandedBatchId)
      .then((detail) => active && setBatchDetail(detail))
      .catch((reason: Error) => active && setError(reason.message))
    void loadDetail()
    const timer = window.setInterval(loadDetail, 2500)
    return () => { active = false; window.clearInterval(timer) }
  }, [expandedBatchId])

  async function cancel(id: string) {
    if (!window.confirm('取消这个任务？已经下载的临时文件会被清理。')) return
    setBusyId(id)
    setError('')
    try {
      await api.cancelJob(id)
      load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取消任务失败，请稍后再试')
    } finally {
      setBusyId('')
    }
  }

  async function remove(id: string) {
    // 提醒保留音轨的损失：开启了「保留原始音视频」的任务，对照音频跟着记录一起删
    if (!window.confirm('删除这条任务记录？临时音轨会一并清理，已保存的文案不受影响。')) return
    setBusyId(id)
    setError('')
    try {
      await api.deleteJob(id)
      load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除任务失败，请稍后再试')
    } finally {
      setBusyId('')
    }
  }

  async function retry(id: string) {
    setBusyId(id)
    setError('')
    try {
      await api.retryJob(id)
      load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '重新提取失败，请稍后再试')
    } finally {
      setBusyId('')
    }
  }

  async function controlBatch(
    id: string,
    action: 'pause' | 'resume' | 'cancel' | 'retry',
  ) {
    if (action === 'cancel' && !window.confirm('取消整个批次？排队中和正在处理的子任务都会停止。')) return
    setBusyId(`batch-${id}`)
    setError('')
    try {
      const detail = action === 'pause'
        ? await api.pauseBatch(id)
        : action === 'resume'
          ? await api.resumeBatch(id)
          : action === 'cancel'
            ? await api.cancelBatch(id)
            : await api.retryFailedBatch(id)
      if (action === 'retry') {
        setExpandedBatchId(detail.id)
        setBatchDetail(detail)
      } else if (expandedBatchId === id) {
        setBatchDetail(detail)
      }
      load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '批次操作失败，请稍后再试')
    } finally {
      setBusyId('')
    }
  }

  // 按状态筛选：任务多时快速定位失败项
  const visibleJobs = jobs.filter((job) => {
    if (job.batch_id) return false
    if (statusFilter === 'active') return job.status === 'queued' || job.status === 'running'
    if (statusFilter === 'failed') return job.status === 'failed' || job.status === 'cancelled'
    if (statusFilter === 'completed') return job.status === 'completed'
    return true
  })
  const visibleBatches = batches.filter((batch) => {
    if (statusFilter === 'active') return ['queued', 'running', 'paused'].includes(batch.status)
    if (statusFilter === 'failed') return ['failed', 'partial_failed', 'cancelled'].includes(batch.status)
    if (statusFilter === 'completed') return batch.status === 'completed'
    return true
  })

  return (
    <div className="page">
      <header className="page-header"><div><h1>任务队列</h1><p>本机正在处理和已经结束的提取任务。</p></div></header>
      <div className="toolbar">
        <label className="filter-select">
          <span className="sr-only">按状态筛选</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="all">全部状态</option>
            <option value="active">进行中</option>
            <option value="completed">已完成</option>
            <option value="failed">失败或已取消</option>
          </select>
        </label>
      </div>
      {error && <p className="form-message error" role="alert">{error}</p>}
      <section className="panel jobs-page-panel">
        {jobs.length === 0 && batches.length === 0 ? (
          <EmptyState icon={ListTodo} title="队列空闲" description="新建一次提取后，可以在这里看到完整进度。" />
        ) : visibleJobs.length === 0 && visibleBatches.length === 0 ? (
          <EmptyState icon={ListTodo} title="这个状态下没有任务" description="把筛选切回「全部状态」看完整队列。" />
        ) : <>
          {visibleBatches.map((batch) => {
            const isExpanded = expandedBatchId === batch.id
            const active = ['queued', 'running', 'paused'].includes(batch.status)
            const detail = isExpanded && batchDetail?.id === batch.id ? batchDetail : null
            return (
              <article className="batch-card" key={batch.id}>
                <div className="batch-card-head">
                  <button
                    className="batch-expand"
                    type="button"
                    onClick={() => {
                      setExpandedBatchId(isExpanded ? '' : batch.id)
                      setBatchDetail(null)
                    }}
                    aria-expanded={isExpanded}
                    aria-label={`${isExpanded ? '收起' : '展开'}批次“${batch.title}”`}
                  >
                    {isExpanded ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
                  </button>
                  <div className="batch-title">
                    <strong>{batch.title}</strong>
                    <small>
                      共 {batch.total_count} 条 · 已完成 {batch.completed_count}
                      {batch.failed_count > 0 && ` · 失败 ${batch.failed_count}`}
                      {batch.cancelled_count > 0 && ` · 取消 ${batch.cancelled_count}`}
                    </small>
                  </div>
                  <span className={`status-chip ${batch.status}`}>{batchStatusLabels[batch.status] ?? batch.status}</span>
                </div>
                <div className="batch-progress-row">
                  <div className="progress-track" aria-label={`批次进度 ${batch.progress}%`}><span style={{ width: `${batch.progress}%` }} /></div>
                  <span>{batch.progress}%</span>
                </div>
                <div className="batch-card-foot">
                  <span>{createdAtFormatter.format(new Date(batch.created_at))}</span>
                  <div className="batch-actions">
                    {batch.control_status === 'active' && active && (
                      <button className="text-button" type="button" onClick={() => controlBatch(batch.id, 'pause')} disabled={busyId === `batch-${batch.id}`}><Pause size={15} /> 暂停</button>
                    )}
                    {batch.control_status === 'paused' && (
                      <button className="text-button" type="button" onClick={() => controlBatch(batch.id, 'resume')} disabled={busyId === `batch-${batch.id}`}><Play size={15} /> 继续</button>
                    )}
                    {!active && batch.failed_count > 0 && (
                      <button className="text-button" type="button" onClick={() => controlBatch(batch.id, 'retry')} disabled={busyId === `batch-${batch.id}`}><RotateCcw size={15} /> 重试失败项</button>
                    )}
                    {active && (
                      <button className="text-button danger" type="button" onClick={() => controlBatch(batch.id, 'cancel')} disabled={busyId === `batch-${batch.id}`}><Ban size={15} /> 取消批次</button>
                    )}
                  </div>
                </div>
                {isExpanded && (
                  <div className="batch-children">
                    {!detail ? (
                      <p className="batch-loading"><LoaderCircle className="spin" size={16} /> 正在读取子任务…</p>
                    ) : detail.jobs.map((job) => {
                      const title = jobDisplayTitle(job, documents)
                      const progress = normalizedProgress(job)
                      const possiblyStalled = isPossiblyStalled(job)
                      return (
                        <div className="batch-child-row" key={job.id}>
                          <span className={`job-state ${job.status}`} aria-hidden="true" />
                          <span className="batch-child-position">#{job.batch_position}</span>
                          <div className="batch-child-main">
                            <div className="batch-child-heading">
                              <span className="batch-child-title" title={job.source_value}>{title}</span>
                              <PlatformBadge platform={job.platform} />
                            </div>
                            <div className="batch-child-detail">
                              <span className="batch-child-stage">{stageLabels[job.stage] ?? job.stage}</span>
                              <span className="batch-child-message" title={job.message}>{job.message}</span>
                              <time dateTime={job.updated_at}>更新于 {updatedAtFormatter.format(new Date(job.updated_at))}</time>
                            </div>
                            {job.status === 'running' && (
                              <div className="batch-child-progress">
                                <div
                                  className="progress-track"
                                  role="progressbar"
                                  aria-label={`第 ${job.batch_position} 项进度`}
                                  aria-valuemin={0}
                                  aria-valuemax={100}
                                  aria-valuenow={progress}
                                >
                                  <span style={{ width: `${progress}%` }} />
                                </div>
                                <span>{progress}%</span>
                              </div>
                            )}
                            {possiblyStalled && (
                              <p className="batch-child-stalled" role="status">
                                <AlertTriangle size={14} aria-hidden="true" />
                                超过 5 分钟没有新进度，可能仍在处理耗时步骤
                              </p>
                            )}
                          </div>
                          <span className={`status-chip ${job.status}`}>{jobStatusLabels[job.status] ?? job.status}</span>
                          {job.status === 'completed' && job.document_id ? (
                            <button className="text-button" type="button" onClick={() => setPreviewId(job.document_id as string)}><FileText size={14} /> 打开</button>
                          ) : null}
                        </div>
                      )
                    })}
                  </div>
                )}
              </article>
            )
          })}
          {visibleJobs.map((job) => {
          const title = jobDisplayTitle(job, documents)
          const cancellable = job.status === 'queued' || job.status === 'running'
          const deletable = !cancellable
          return (
            <article className="job-card" key={job.id}>
              <div className="job-card-top">
                <div className="job-title">
                  <span className={`job-state ${job.status}`} />
                  <div>
                    <strong title={title}>{title}</strong>
                    {job.status !== 'completed' && <small title={job.source_value}>{job.message}</small>}
                    {job.error_code?.startsWith('COOKIE_') && (
                      <Link className="job-cookie-hint" to="/connectors">去「平台连接」获取访问权限</Link>
                    )}
                  </div>
                </div>
                <div className="job-actions">
                  <PlatformBadge platform={job.platform} />
                  <span className={`status-chip ${job.status}`}>{jobStatusLabels[job.status] ?? job.status}</span>
                </div>
              </div>
              {job.status === 'running' && (
                <div className="progress-track" aria-label={`进度 ${job.progress}%`}><span style={{ width: `${job.progress}%` }} /></div>
              )}
              <div className="job-card-foot">
                <div className="job-card-meta">
                  <span className="job-meta-date">{createdAtFormatter.format(new Date(job.created_at))}</span>
                  <span className="job-meta-source" title={job.transcript_source ? sourceHint(job) : undefined}>
                    {job.transcript_source
                      ? `来源：${job.transcript_source === 'subtitle' ? '平台字幕' : '本地识别'}`
                      : ''}
                  </span>
                  {job.status !== 'completed' && <span className="job-meta-stage">阶段：{stageLabels[job.stage] ?? job.stage}</span>}
                  {job.model_name && (
                    <span className="job-model" title={job.model_name}>
                      <Cpu size={13} />
                      {shortModelName(job.model_name)}
                    </span>
                  )}
                </div>
                <div className="job-card-actions">
                  {/* 失败/取消的任务给一键重试，省去回工作台重新粘贴链接 */}
                  {deletable && job.status !== 'completed' && (
                    <button className="text-button" type="button" onClick={() => retry(job.id)} disabled={busyId === job.id}><RotateCcw size={15} /> 重新提取</button>
                  )}
                  {cancellable && (
                    <button className="text-button danger" type="button" onClick={() => cancel(job.id)} disabled={busyId === job.id}><Ban size={15} /> 取消任务</button>
                  )}
                  {deletable && (
                    <button className="icon-button danger job-delete" type="button" onClick={() => remove(job.id)} disabled={busyId === job.id} aria-label={`删除任务《${title}》`} title="删除记录"><Trash2 size={15} /></button>
                  )}
                  {job.status === 'completed' && job.document_id && (
                    <button className="secondary-button" type="button" onClick={() => setPreviewId(job.document_id as string)}><FileText size={15} /> 打开文案</button>
                  )}
                </div>
              </div>
            </article>
          )
          })}
        </>}
      </section>
      {previewId && (
        <DocumentPreviewModal key={previewId} documentId={previewId} backTo="/jobs" backLabel="任务队列" onClose={() => setPreviewId('')} />
      )}
    </div>
  )
}
