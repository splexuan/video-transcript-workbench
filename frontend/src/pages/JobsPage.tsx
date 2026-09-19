import { Ban, Cpu, FileText, ListTodo, RotateCcw, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { DocumentPreviewModal } from '../components/DocumentPreviewModal'
import { EmptyState } from '../components/EmptyState'
import { PlatformBadge } from '../components/PlatformBadge'
import { api } from '../lib/api'
import { jobDisplayTitle } from '../lib/jobDisplay'
import { jobStatusLabels, shortModelName, stageLabels } from '../lib/labels'
import type { DocumentSummary, Job } from '../types'

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

export function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [error, setError] = useState('')
  // 「打开文案」先弹窗预览，要改再进编辑页
  const [previewId, setPreviewId] = useState('')
  // 正在执行取消/删除/重试的任务：按钮禁用，避免重复点击
  const [busyId, setBusyId] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')

  function load() {
    Promise.all([api.jobs(), api.documents()])
      .then(([nextJobs, nextDocuments]) => {
        setJobs(nextJobs)
        setDocuments(nextDocuments)
        setError('')
      })
      .catch((reason: Error) => setError(reason.message))
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 2500)
    return () => window.clearInterval(timer)
  }, [])

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

  // 按状态筛选：任务多时快速定位失败项
  const visibleJobs = jobs.filter((job) => {
    if (statusFilter === 'active') return job.status === 'queued' || job.status === 'running'
    if (statusFilter === 'failed') return job.status === 'failed' || job.status === 'cancelled'
    if (statusFilter === 'completed') return job.status === 'completed'
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
        {jobs.length === 0 ? (
          <EmptyState icon={ListTodo} title="队列空闲" description="新建一次提取后，可以在这里看到完整进度。" />
        ) : visibleJobs.length === 0 ? (
          <EmptyState icon={ListTodo} title="这个状态下没有任务" description="把筛选切回「全部状态」看完整队列。" />
        ) : visibleJobs.map((job) => {
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
      </section>
      {previewId && (
        <DocumentPreviewModal key={previewId} documentId={previewId} backTo="/jobs" backLabel="任务队列" onClose={() => setPreviewId('')} />
      )}
    </div>
  )
}
