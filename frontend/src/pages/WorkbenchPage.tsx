import { ArrowRight, CircleCheck, FileUp, Link2, ListPlus, LoaderCircle, ShieldCheck, Sparkles, TriangleAlert } from 'lucide-react'
import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { BatchExecution } from '../components/BatchExecution'
import { DocumentPreviewModal } from '../components/DocumentPreviewModal'
import { PlatformBadge } from '../components/PlatformBadge'
import { TaskExecution } from '../components/TaskExecution'
import { api } from '../lib/api'
import { jobDisplayTitle } from '../lib/jobDisplay'
import { isRunningBatch } from '../lib/jobBatch'
import { shortModelName } from '../lib/labels'
import type { DocumentSummary, DocumentTitle, Job, JobBatch, JobBatchDetail, JobBatchPreflight, ModelCatalog, RecognitionModel } from '../types'

function timeLabel(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}

function sizeLabel(bytes: number) {
  return bytes >= 1024 ** 3
    ? `${(bytes / 1024 ** 3).toFixed(1)} GB`
    : `${Math.round(bytes / 1024 ** 2)} MB`
}

function batchLines(value: string) {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
}

/** 卡片副标题：这个模型是什么模式、时间轴多细、多大。 */
function modelSubtitle(model: RecognitionModel) {
  const kind = model.tier === 'fast' ? '极速文本' : '精准时间轴'
  const detail = model.tier === 'fast' ? '30 秒粒度' : '句级时间戳'
  const size = sizeLabel(model.approx_bytes)
  return model.state === 'ready'
    ? `${kind} · ${detail} · ${size}`
    : `${kind} · ${size} · 未安装`
}

export function WorkbenchPage() {
  const [captureMode, setCaptureMode] = useState<'single' | 'batch'>('single')
  const [source, setSource] = useState('')
  const [batchSource, setBatchSource] = useState('')
  const [batchPreflight, setBatchPreflight] = useState<JobBatchPreflight | null>(null)
  const [preflighting, setPreflighting] = useState(false)
  const [preflightError, setPreflightError] = useState('')
  const [modelId, setModelId] = useState('')
  const [preferSubtitle, setPreferSubtitle] = useState(true)
  const [jobs, setJobs] = useState<Job[]>([])
  // 右栏「最近文案」只展示 5 条，所以只取第一页，不再把整个文案库拉回来
  const [recentDocuments, setRecentDocuments] = useState<DocumentSummary[]>([])
  // 任务行、批次行要显示的标题：只按当前涉及的 document_id 查
  const [titles, setTitles] = useState<DocumentTitle[]>([])
  // 最近文案点击 → 文案预览弹窗
  const [docPreviewId, setDocPreviewId] = useState('')
  // 本次会话刚提交的任务：刷新页面后清空，任务执行面板不会回显历史任务
  const [focusedJobId, setFocusedJobId] = useState('')
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [createdBatchId, setCreatedBatchId] = useState('')
  // 服务端最近批次的摘要：右栏要靠它认出「正在跑的那个批次」，不能只认本地刚提交的 id
  const [recentBatches, setRecentBatches] = useState<JobBatch[]>([])
  // 当前展示的批次详情：批量执行时右栏要逐条显示子任务，只有 id 看不到每条的状态
  const [batchDetail, setBatchDetail] = useState<JobBatchDetail | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let active = true
    const load = () => Promise.all([
      api.jobs({ limit: 4 }),
      api.documents({ limit: 5 }),
      api.batches({ limit: 20 }),
    ])
      .then(([jobsPage, documentsPage, batchesPage]) => {
        if (!active) return
        setJobs(jobsPage.items)
        setRecentDocuments(documentsPage.items)
        setRecentBatches(batchesPage.items)
      })
      .catch((reason: Error) => active && setError(reason.message))
    void load()
    const timer = window.setInterval(load, 1600)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  // 右栏要盯的批次：本次会话提交的优先（已结束也继续显示结果，用户刚提交就想看结论），
  // 否则回落到服务端「正在推进」的最新批次——切页面再回来、或刷新页面时，正在跑的批量
  // 任务不会从右栏消失；已结束与暂停的都不回落，避免把历史批次或无法收掉的暂停批次钉在首页。
  const runningBatch = recentBatches.find((batch) => isRunningBatch(batch.status))
  const monitoredBatchId = createdBatchId || runningBatch?.id || ''

  // 批次的子任务详情单独轮询。没有可监视的批次时（提交的是单条任务，或没有在跑的批次）
  // 不发请求，右栏回到原来的单任务形态。
  useEffect(() => {
    if (!monitoredBatchId) return
    let active = true
    const load = () => api.batch(monitoredBatchId)
      .then((detail) => active && setBatchDetail(detail))
      .catch((reason: Error) => active && setError(reason.message))
    void load()
    const timer = window.setInterval(load, 1600)
    return () => { active = false; window.clearInterval(timer) }
  }, [monitoredBatchId])

  // 只查当前这几条任务需要的文案标题。原来是把整个文案库拉下来在本地查找，文案一多，
  // 1.6 秒一次的轮询就成了实打实的负担；这里改成按 document_id 精确取。
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
    if (captureMode !== 'batch') return
    const sources = batchLines(batchSource)
    if (sources.length === 0 || sources.length > 50) return
    let active = true
    const timer = window.setTimeout(() => {
      setPreflighting(true)
      setPreflightError('')
      void api.preflightBatch(sources)
        .then((result) => active && setBatchPreflight(result))
        .catch((reason: Error) => active && setPreflightError(reason.message))
        .finally(() => active && setPreflighting(false))
    }, 350)
    return () => { active = false; window.clearTimeout(timer) }
  }, [batchSource, captureMode])

  function selectCaptureMode(next: 'single' | 'batch') {
    setCaptureMode(next)
    setBatchPreflight(null)
    setPreflightError('')
    setPreflighting(false)
  }

  function changeBatchSource(value: string) {
    setBatchSource(value)
    setBatchPreflight(null)
    setPreflightError('')
    setPreflighting(false)
  }

  useEffect(() => {
    let active = true
    Promise.allSettled([api.models(), api.settings()])
      .then(([catalogResult, settingsResult]) => {
        if (!active) return
        const nextCatalog = catalogResult.status === 'fulfilled' ? catalogResult.value : null
        setCatalog(nextCatalog)
        if (settingsResult.status !== 'fulfilled') return
        setPreferSubtitle(settingsResult.value.prefer_subtitle)
        // 默认选中设置里的模型；它没装就退到推荐模型，再退到任意已装模型
        const preferred = settingsResult.value.default_model
        const installed = (nextCatalog?.models ?? []).filter((item) => item.state === 'ready')
        const pick = installed.find((item) => item.id === preferred)
          ?? installed.find((item) => item.recommended)
          ?? installed[0]
        setModelId(pick?.id ?? '')
      })
    return () => { active = false }
  }, [])

  const models = catalog?.models ?? []
  const missingModels = models.filter((item) => item.state !== 'ready')
  const readyModels = models.filter((item) => item.state === 'ready')
  const selectedModel = models.find((item) => item.id === modelId)

  function submitMode() {
    // mode 只是给旧逻辑的兜底值，后端会按所选的模型推导
    return modelId.startsWith('faster-whisper') ? 'accurate' : 'fast'
  }

  function togglePreferSubtitle(next: boolean) {
    setPreferSubtitle(next)
    // 开关状态记到设置里，下次打开保持上次的选择
    void api.updateSettings({ prefer_subtitle: next }).catch(() => undefined)
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const sources = batchLines(batchSource)
    if (captureMode === 'single' && !source.trim()) return
    if (captureMode === 'batch' && (!batchPreflight?.can_submit || sources.length === 0)) return
    setSubmitting(true)
    setError('')
    setNotice('')
    setCreatedBatchId('')
    setBatchDetail(null)
    try {
      if (captureMode === 'batch') {
        const created = await api.createBatch({
          sources,
          mode: submitMode(),
          model_id: modelId || null,
          prefer_subtitle: preferSubtitle,
          client_request_id: crypto.randomUUID(),
        })
        setJobs((current) => [...created.jobs, ...current].slice(0, 4))
        setFocusedJobId(created.jobs[0]?.id ?? '')
        setBatchSource('')
        setBatchPreflight(null)
        setCreatedBatchId(created.id)
        setNotice(`已创建批次，共 ${created.total_count} 条链接。`)
        return
      }
      const created = await api.createJob({
        source_type: 'url',
        source,
        mode: submitMode(),
        model_id: modelId || null,
        prefer_subtitle: preferSubtitle,
      })
      setJobs((current) => [created, ...current].slice(0, 4))
      setFocusedJobId(created.id)
      setSource('')
      setNotice('已加入队列，完成后会自动归档到文案库。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建任务失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setUploading(true)
    setError('')
    setNotice('')
    try {
      const created = await api.uploadJob(file, submitMode(), modelId || null, preferSubtitle)
      setJobs((current) => [created, ...current].slice(0, 4))
      setFocusedJobId(created.id)
      setNotice(`“${file.name}”已加入队列。`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导入失败，请确认文件可用')
    } finally {
      setUploading(false)
    }
  }

  // 任务执行优先显示正在跑的任务；其次是本次会话刚提交的那条（含完成/失败结果）。
  // 刷新页面后两个来源都为空，面板回到空态，不会回显上次的任务。
  const activeJob = jobs.find((job) => job.status === 'queued' || job.status === 'running')
  const focusedJob = focusedJobId ? jobs.find((job) => job.id === focusedJobId) : undefined
  const current = activeJob ?? focusedJob ?? null

  async function retryJob(id: string) {
    setError('')
    try {
      const created = await api.retryJob(id)
      setJobs((current) => [created, ...current].slice(0, 4))
      setFocusedJobId(created.id)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '重新提取失败，请稍后再试')
    }
  }

  return (
    <div className="page workbench-page">
      <header className="page-header hero-header">
        <div>
          <h1>把视频，提取出可用的文案</h1>
          <p>粘贴链接或导入本地文件。字幕抓取、语音识别、校对都在你这台电脑上完成。</p>
        </div>
        <div className="local-chip"><ShieldCheck size={16} /> 纯本地处理</div>
      </header>

      {/* 左列：提取 + 最近文案；右列：任务动态竖条 */}
      <div className="workbench-grid">
        <div className="workbench-main">
      <section className="capture-card" aria-labelledby="capture-title">
        <div className="capture-heading">
          <span className="capture-icon"><Link2 size={20} /></span>
          <div>
            <h2 id="capture-title">开始一次提取</h2>
            <p>支持 B站、抖音、快手、小红书、视频号的链接或分享文案，也可以导入本地音视频文件</p>
          </div>
        </div>
        <div className="capture-mode-switch" role="radiogroup" aria-label="提取方式">
          <button
            type="button"
            role="radio"
            aria-checked={captureMode === 'single'}
            className={captureMode === 'single' ? 'active' : ''}
            onClick={() => selectCaptureMode('single')}
          >
            <Link2 size={16} /> 单条提取
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={captureMode === 'batch'}
            className={captureMode === 'batch' ? 'active' : ''}
            onClick={() => selectCaptureMode('batch')}
          >
            <ListPlus size={16} /> 批量提取
          </button>
        </div>
        <form onSubmit={submit}>
          {captureMode === 'single' ? (
            <>
              <label className="sr-only" htmlFor="source-url">视频链接或分享文案</label>
              <div className="source-input-wrap">
                {/* 用 text 而不是 url：App 里复制出来的整段分享文案含中文，
                    浏览器的 url 校验会直接拦下提交；链接由后端从文案里提取。 */}
                <input
                  id="source-url"
                  type="text"
                  value={source}
                  onChange={(event) => setSource(event.target.value)}
                  placeholder="粘贴 B站、抖音、快手、小红书、视频号链接或分享文案"
                  autoComplete="off"
                  required
                />
                <button className="primary-button" type="submit" disabled={submitting || !source.trim()}>
                  {submitting ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}
                  {submitting ? '正在提交' : '开始提取'}
                </button>
              </div>
            </>
          ) : (
            <div className="batch-source-wrap">
              <label htmlFor="batch-source">每行粘贴一条视频链接或分享文案</label>
              <textarea
                id="batch-source"
                value={batchSource}
                onChange={(event) => changeBatchSource(event.target.value)}
                placeholder={'https://www.bilibili.com/video/...\nhttps://v.douyin.com/...\n每行一条，最多 50 条'}
                rows={7}
              />
              <div className="batch-submit-row">
                <span>{batchLines(batchSource).length}/50 条</span>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={submitting || preflighting || !batchPreflight?.can_submit}
                >
                  {submitting ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}
                  {submitting ? '正在创建批次' : `开始批量提取${batchPreflight?.can_submit ? `（${batchPreflight.valid_count}）` : ''}`}
                </button>
              </div>
              {preflighting && <p className="batch-preflight pending"><LoaderCircle className="spin" size={15} /> 正在检查链接…</p>}
              {!preflighting && batchPreflight?.can_submit && (
                <p className="batch-preflight valid"><CircleCheck size={15} /> {batchPreflight.valid_count} 条链接均可提交</p>
              )}
              {!preflighting && batchPreflight && !batchPreflight.can_submit && (
                <div className="batch-preflight issues" role="alert">
                  <p><TriangleAlert size={15} /> 请先修正以下条目</p>
                  <ul>
                    {batchPreflight.items.filter((item) => item.status !== 'valid').map((item) => (
                      <li key={`${item.position}-${item.status}`}>第 {item.position} 行：{item.message}</li>
                    ))}
                  </ul>
                </div>
              )}
              {batchLines(batchSource).length > 50 && (
                <p className="batch-preflight issues" role="alert"><TriangleAlert size={15} /> 单个批次最多 50 条，请拆成多个批次提交。</p>
              )}
              {preflightError && <p className="batch-preflight issues" role="alert"><TriangleAlert size={15} /> {preflightError}</p>}
            </div>
          )}
          <div className="capture-options">
            <details className="model-picker-disclosure">
              <summary><span>识别模型</span><strong>{selectedModel ? shortModelName(selectedModel.name) : '尚未选择'}</strong><small>更换</small></summary>
              <div className="model-picker" role="radiogroup" aria-label="识别模型">
                {models.map((model) => {
                  const ready = model.state === 'ready'
                  const name = shortModelName(model.name)
                  return (
                    <button
                      key={model.id}
                      type="button"
                      role="radio"
                      aria-checked={modelId === model.id}
                      className={modelId === model.id ? 'model-option active' : 'model-option'}
                      onClick={() => setModelId(model.id)}
                      disabled={!ready}
                      title={ready
                        ? `没有字幕时用 ${name} 识别。`
                        : `${name} 还没安装，去「模型管理」页下载后才能选中。`}
                    >
                      <span className="model-option-title">
                        {name}
                        <span className={`model-dot ${ready ? 'ready' : 'missing'}`} aria-hidden="true" />
                      </span>
                      <span className="model-option-sub">{modelSubtitle(model)}</span>
                    </button>
                  )
                })}
              </div>
            </details>
            <input
              ref={fileInput}
              hidden
              type="file"
              accept="audio/*,video/*,.mkv,.flac,.opus"
              onChange={upload}
            />
            {captureMode === 'single' ? (
              <button className="file-button" type="button" onClick={() => fileInput.current?.click()} disabled={uploading}>
                {uploading ? <LoaderCircle className="spin" size={16} /> : <FileUp size={16} />}
                {uploading ? '正在导入' : '导入本地文件'}
              </button>
            ) : <span className="batch-file-note">首版批量模式仅支持链接</span>}
          </div>
          <div className="subtitle-toggle">
            {/* 只有这一行是控件区，下面的说明是纯文本，读说明时不会误触 */}
            <label className="subtitle-toggle-head" htmlFor="prefer-subtitle">
              <strong>优先使用平台字幕</strong>
              <input
                id="prefer-subtitle"
                type="checkbox"
                checked={preferSubtitle}
                onChange={(event) => togglePreferSubtitle(event.target.checked)}
              />
              <span className="toggle" aria-hidden="true" />
            </label>
            <small>
              {preferSubtitle
                ? '有字幕就直接读取（含 B站 AI 字幕），不下载音轨转写，速度最快；代价是这类字幕没有标点，文案按一行一句展示。'
                : `已关闭：跳过平台字幕，始终用 ${modelId ? shortModelName(models.find((item) => item.id === modelId)?.name ?? '') : '所选模型'} 重新转写，文案自带标点和分段。`}
            </small>
          </div>
          {missingModels.length > 0 && (
            <p className="capture-hint">
              {readyModels.length === 0 && '还没有可用的识别模型，'}
              {missingModels.map((item) => shortModelName(item.name)).join('、')} 未安装，去 <Link to="/models">模型管理</Link> 下载后即可选中；
              {preferSubtitle ? '只提取自带字幕的视频不受影响。' : '当前已关闭平台字幕，必须先装好模型才能转写。'}
            </p>
          )}
          {/* 模型未装好时上面那条已经说了「先装模型」，不再重复第二遍 */}
          {missingModels.length === 0 && !preferSubtitle && !modelId && (
            <p className="capture-hint">
              已关闭平台字幕，但还没有选择识别模型：请到 <Link to="/models">模型管理</Link> 里选一个，或重新打开「优先使用平台字幕」。
            </p>
          )}
          {error && <p className="form-message error" role="alert">{error}</p>}
          {notice && (
            <p className="form-message success" role="status">
              {notice} {createdBatchId && <Link to={`/jobs?batch=${createdBatchId}`}>查看批次</Link>}
            </p>
          )}
        </form>
      </section>

        <section className="panel">
          <div className="section-heading">
            <div><h2>最近文案</h2><p>接着上次继续校对</p></div>
            <Link to="/library">查看全部 <ArrowRight size={15} /></Link>
          </div>
          {recentDocuments.length === 0 ? (
            <div className="compact-empty">
              <span>还没有文案</span>
              <p>提取完成后会自动保存在这里。</p>
            </div>
          ) : (
            <div className="document-list">
              {recentDocuments.map((document) => (
                <Link
                  className="document-row"
                  to={`/documents/${document.id}`}
                  state={{ from: '/', label: '工作台' }}
                  key={document.id}
                  onClick={(event) => { event.preventDefault(); setDocPreviewId(document.id) }}
                >
                  <span className="document-glyph">稿</span>
                  <span className="document-info">
                    <strong>{document.title}</strong>
                    <small>{timeLabel(document.updated_at)} · {document.word_count} 字</small>
                  </span>
                  <PlatformBadge platform={document.platform} />
                  <ArrowRight size={16} />
                </Link>
              ))}
            </div>
          )}
        </section>
        </div>

        <section className="panel task-panel">
          <div className="section-heading">
            <div>
              <h2>任务执行</h2>
              <p>{batchDetail ? '批次里每一条的实时进度' : '当前提取任务的实时进度'}</p>
            </div>
            <Link to="/jobs">全部任务 <ArrowRight size={15} /></Link>
          </div>
          {/* 批量任务要逐条显示子任务：单任务面板（TaskExecution）只够放一条，
              批次跑起来时用它只能看到最先提交的那条，看不到其余进度。 */}
          {batchDetail ? (
            <BatchExecution
              batch={batchDetail}
              titles={titles}
              onOpenDocument={setDocPreviewId}
            />
          ) : current ? (
            <TaskExecution
              job={current}
              title={jobDisplayTitle(current, titles)}
              onOpenDocument={setDocPreviewId}
              onRetry={retryJob}
            />
          ) : (
            <div className="compact-empty"><span>没有正在执行的任务</span><p>提交提取后，这里会实时显示每一步进度。</p></div>
          )}
        </section>
      </div>
      {docPreviewId && (
        <DocumentPreviewModal key={docPreviewId} documentId={docPreviewId} backTo="/" backLabel="工作台" onClose={() => setDocPreviewId('')} />
      )}
    </div>
  )
}
