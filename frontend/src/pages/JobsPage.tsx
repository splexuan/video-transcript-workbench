import { AlertTriangle, Ban, ChevronDown, ChevronRight, ChevronUp, Cpu, ListTodo, LoaderCircle, Pause, Play, RotateCcw } from 'lucide-react'
import { useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { DocumentPreviewModal } from '../components/DocumentPreviewModal'
import { EmptyState } from '../components/EmptyState'
import { PlatformBadge } from '../components/PlatformBadge'
import { api } from '../lib/api'
import { batchStatusLabels, isOpenBatch, isPossiblyStalled, normalizedProgress, updatedAtFormatter } from '../lib/jobBatch'
import { jobDisplayTitle } from '../lib/jobDisplay'
import { jobStatusLabels, shortModelName, stageLabels } from '../lib/labels'
import type { DocumentTitle, Job, JobBatch, JobBatchDetail } from '../types'

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

/** 每页条数：队列是流水列表，一页够看一屏历史即可。 */
const PAGE_SIZE = 20

/**
 * 把一页并进已有列表，按 id 去重。
 *
 * `head` 用于「第一页常刷新」：新数据放前面，已经翻出来的旧页保持在后；
 * `tail` 用于「加载更多」：新一页接在末尾。两种方向共用同一套去重规则，
 * 免得翻页途中数据变动导致同一行出现两次。
 */
function mergePage<T extends { id: string }>(page: T[], current: T[], position: 'head' | 'tail'): T[] {
  const incoming = new Set(page.map((item) => item.id))
  return position === 'head'
    ? [...page, ...current.filter((item) => !incoming.has(item.id))]
    : [...current, ...page.filter((item) => !incoming.has(item.id))]
}

/** 界面上的状态筛选 → 后端状态白名单；返回 undefined 表示不筛。 */
function statusQuery(filter: string): string[] | undefined {
  if (filter === 'active') return ['queued', 'running']
  if (filter === 'failed') return ['failed', 'cancelled']
  if (filter === 'completed') return ['completed']
  return undefined
}

/**
 * 批次是另一套状态取值，不能复用任务那套口径：
 * 暂停的批次还没结束、仍可继续，算「进行中」；部分失败的批次算「失败」。
 */
function batchStatusQuery(filter: string): string[] | undefined {
  if (filter === 'active') return ['queued', 'running', 'paused']
  if (filter === 'failed') return ['failed', 'partial_failed', 'cancelled']
  if (filter === 'completed') return ['completed']
  return undefined
}

export function JobsPage() {
  const [searchParams] = useSearchParams()
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobCursor, setJobCursor] = useState<string | null>(null)
  const [batches, setBatches] = useState<JobBatch[]>([])
  const [batchCursor, setBatchCursor] = useState<string | null>(null)
  const [expandedBatchId, setExpandedBatchId] = useState(searchParams.get('batch') ?? '')
  const [batchDetail, setBatchDetail] = useState<JobBatchDetail | null>(null)
  // 任务行要显示的文案标题：按涉及的 document_id 精确查，不再拉整个文案库
  const [titles, setTitles] = useState<DocumentTitle[]>([])
  const [error, setError] = useState('')
  // 正在「加载更多」的列表（空串表示没有）
  const [loadingMore, setLoadingMore] = useState('')
  // 「打开文案」先弹窗预览，要改再进编辑页
  const [previewId, setPreviewId] = useState('')
  // 正在执行取消/删除/重试的任务：按钮禁用，避免重复点击
  const [busyId, setBusyId] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')

  // 第一页定时刷新（状态要实时），已经翻出来的旧页接在后面——否则每 2.5 秒都会把
  // 用户点出来的历史冲掉。原来的做法是一次拉 200 条，第 201 条在界面上永远看不到。
  function refresh(reset = false) {
    Promise.all([
      api.jobs({
        // 批次子任务归批次卡片展示，要在服务端排掉：否则一页的配额被它们吃掉大半
        standalone: true,
        status: statusQuery(statusFilter),
        limit: PAGE_SIZE,
      }),
      api.batches({ status: batchStatusQuery(statusFilter), limit: PAGE_SIZE }),
    ])
      .then(([jobsPage, batchesPage]) => {
        if (reset) {
          // 换筛选：旧条件下的页不能留在新列表里，游标也从第一页重来
          setJobs(jobsPage.items)
          setBatches(batchesPage.items)
          setJobCursor(jobsPage.next_cursor)
          setBatchCursor(batchesPage.next_cursor)
        } else {
          setJobs((current) => mergePage(jobsPage.items, current, 'head'))
          setBatches((current) => mergePage(batchesPage.items, current, 'head'))
          // 游标只补一次：已经翻过页就不能再用第一页的游标覆盖，那会跳过中间几页
          setJobCursor((current) => current ?? jobsPage.next_cursor)
          setBatchCursor((current) => current ?? batchesPage.next_cursor)
        }
        setError('')
      })
      .catch((reason: Error) => setError(reason.message || '读取任务失败'))
  }

  useEffect(() => {
    refresh(true)
    const timer = window.setInterval(() => refresh(), 2500)
    return () => window.clearInterval(timer)
    // refresh 每次渲染都会重建，放进依赖会让定时器反复重装；真正要跟的是筛选条件
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter])

  async function loadMore(kind: 'jobs' | 'batches') {
    const cursor = kind === 'jobs' ? jobCursor : batchCursor
    if (!cursor || loadingMore) return
    setLoadingMore(kind)
    setError('')
    try {
      if (kind === 'jobs') {
        const page = await api.jobs({
          standalone: true,
          status: statusQuery(statusFilter),
          limit: PAGE_SIZE,
          cursor,
        })
        setJobs((current) => mergePage(page.items, current, 'tail'))
        setJobCursor(page.next_cursor)
      } else {
        const page = await api.batches({ status: batchStatusQuery(statusFilter), limit: PAGE_SIZE, cursor })
        setBatches((current) => mergePage(page.items, current, 'tail'))
        setBatchCursor(page.next_cursor)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '加载更多失败，请稍后再试')
    } finally {
      setLoadingMore('')
    }
  }

  const titleIds = useMemo(() => {
    const ids = new Set<string>()
    for (const job of [...jobs, ...(batchDetail?.jobs ?? [])]) {
      if (job.document_id) ids.add(job.document_id)
    }
    return [...ids].sort()
  }, [jobs, batchDetail])
  const titleKey = titleIds.join(',')

  useEffect(() => {
    if (!titleKey) return
    let active = true
    api.documentTitles(titleKey.split(','))
      .then((items) => active && setTitles(items))
      .catch(() => undefined)
    return () => { active = false }
  }, [titleKey])

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
      void refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取消任务失败，请稍后再试')
    } finally {
      setBusyId('')
    }
  }

  async function retry(id: string) {
    setBusyId(id)
    setError('')
    try {
      await api.retryJob(id)
      void refresh()
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
      void refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '批次操作失败，请稍后再试')
    } finally {
      setBusyId('')
    }
  }

  // 任务与批次的筛选都已在服务端做完（批次状态是派生的，服务端边扫边算，
  // 见后端 list_job_batches），`jobs` 与 `batches` 就是可见结果，本地不再过滤。

  return (
    <div className="page">
      <header className="page-header"><div><h1>任务队列</h1><p>本机正在处理和已经结束的提取任务。</p></div></header>
      <div className="toolbar">
        <div className="toolbar-filters">
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
      </div>
      {error && <p className="form-message error" role="alert">{error}</p>}
      <section className="panel jobs-page-panel">
        {jobs.length === 0 && batches.length === 0 ? (
          <EmptyState icon={ListTodo} title="队列空闲" description="新建一次提取后，可以在这里看到完整进度。" />
        ) : jobs.length === 0 && batches.length === 0 ? (
          <EmptyState icon={ListTodo} title="这个状态下没有任务" description="把筛选切回「全部状态」看完整队列。" />
        ) : <>
          {batches.map((batch) => {
            const isExpanded = expandedBatchId === batch.id
            const active = isOpenBatch(batch.status)
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
                      const title = jobDisplayTitle(job, titles)
                      const progress = normalizedProgress(job)
                      const possiblyStalled = isPossiblyStalled(job)
                      // 识别完、有文案的子任务整行可点，直接打开预览；还在跑或失败的行
                      // 不接受点击，免得点上去没反应、看起来像坏了
                      const documentId = job.status === 'completed' ? job.document_id : null
                      return (
                        <div
                          className={documentId ? 'batch-child-row clickable' : 'batch-child-row'}
                          key={job.id}
                          role={documentId ? 'button' : undefined}
                          tabIndex={documentId ? 0 : undefined}
                          aria-label={documentId ? `打开文案《${title}》` : undefined}
                          onClick={documentId ? () => setPreviewId(documentId) : undefined}
                          onKeyDown={
                            documentId
                              ? (event: ReactKeyboardEvent<HTMLDivElement>) => {
                                  // 空格在容器上默认是滚动页面，得自己拦下来
                                  if (event.key !== 'Enter' && event.key !== ' ') return
                                  event.preventDefault()
                                  setPreviewId(documentId)
                                }
                              : undefined
                          }
                        >
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
                          {/* 行尾箭头只给「点得开」的行，作为可点的视觉提示 */}
                          {documentId && <ChevronRight className="batch-child-open" size={16} aria-hidden="true" />}
                        </div>
                      )
                    })}
                  </div>
                )}
              </article>
            )
          })}
          {batchCursor && (
            <div className="list-more">
              <button className="secondary-button" type="button" onClick={() => void loadMore('batches')} disabled={loadingMore === 'batches'}>
                {loadingMore === 'batches' ? <LoaderCircle className="spin" size={16} /> : <ChevronDown size={16} />}
                {loadingMore === 'batches' ? '正在加载' : '加载更多批次'}
              </button>
            </div>
          )}
          {jobs.map((job) => {
          const title = jobDisplayTitle(job, titles)
          const cancellable = job.status === 'queued' || job.status === 'running'
          // 失败或取消的任务给一键重试，省去回工作台重新粘贴链接
          const retryable = job.status === 'failed' || job.status === 'cancelled'
          // 识别完、有文案的任务整行可点，直接打开预览；其余行不接受点击
          const documentId = job.status === 'completed' ? job.document_id : null
          return (
            <article
              className={documentId ? 'job-card clickable' : 'job-card'}
              key={job.id}
              role={documentId ? 'button' : undefined}
              tabIndex={documentId ? 0 : undefined}
              aria-label={documentId ? `打开文案《${title}》` : undefined}
              onClick={documentId ? () => setPreviewId(documentId) : undefined}
              onKeyDown={
                documentId
                  ? (event: ReactKeyboardEvent<HTMLElement>) => {
                      // 空格在容器上默认是滚动页面，得自己拦下来
                      if (event.key !== 'Enter' && event.key !== ' ') return
                      event.preventDefault()
                      setPreviewId(documentId)
                    }
                  : undefined
              }
            >
              <div className="job-card-top">
                <div className="job-title">
                  <span className={`job-state ${job.status}`} />
                  <div>
                    <strong title={title}>{title}</strong>
                    {job.status !== 'completed' && <small title={job.source_value}>{job.message}</small>}
                    {job.error_code?.startsWith('COOKIE_') && (
                      <Link className="job-cookie-hint" to="/connectors" onClick={(event) => event.stopPropagation()}>去「平台连接」获取访问权限</Link>
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
                  {/* 卡片整行可点，行内按钮要把点击拦下来，别顺手把文案也打开了 */}
                  {retryable && (
                    <button className="text-button" type="button" onClick={(event) => { event.stopPropagation(); void retry(job.id) }} disabled={busyId === job.id}><RotateCcw size={15} /> 重新提取</button>
                  )}
                  {cancellable && (
                    <button className="text-button danger" type="button" onClick={(event) => { event.stopPropagation(); void cancel(job.id) }} disabled={busyId === job.id}><Ban size={15} /> 取消任务</button>
                  )}
                </div>
              </div>
            </article>
          )
          })}
          {jobCursor && (
            <div className="list-more">
              <button className="secondary-button" type="button" onClick={() => void loadMore('jobs')} disabled={loadingMore === 'jobs'}>
                {loadingMore === 'jobs' ? <LoaderCircle className="spin" size={16} /> : <ChevronDown size={16} />}
                {loadingMore === 'jobs' ? '正在加载' : '加载更多任务'}
              </button>
            </div>
          )}
        </>}
      </section>
      {previewId && (
        <DocumentPreviewModal key={previewId} documentId={previewId} backTo="/jobs" backLabel="任务队列" onClose={() => setPreviewId('')} />
      )}
    </div>
  )
}
