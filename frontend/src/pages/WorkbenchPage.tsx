import { ArrowRight, CircleCheck, Eye, FileUp, Link2, ListPlus, LoaderCircle, ShieldCheck, Sparkles, TriangleAlert, X } from 'lucide-react'
import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { BatchExecution } from '../components/BatchExecution'
import { DocumentOverlay, type OverlayMode } from '../components/DocumentOverlay'
import { PlatformBadge } from '../components/PlatformBadge'
import { TaskExecution } from '../components/TaskExecution'
import { Thumb } from '../components/Thumb'
import { api } from '../lib/api'
import { jobDisplayTitle } from '../lib/jobDisplay'
import { isRunningBatch } from '../lib/jobBatch'
import { recognitionModels } from '../lib/models'
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

/**
 * 可以导入的本地音视频后缀，与后端 /api/jobs/upload 的白名单保持一致。
 *
 * 后端才是权威（它会按扩展名拒掉不支持的格式），这里提前一份是为了：拖进来时立刻
 * 能提示「不支持这个格式」，不必先上传一大段再被退回；文件选择框的 accept 也用它。
 */
const MEDIA_SUFFIXES = [
  'mp4', 'mov', 'mkv', 'webm', 'avi',
  'mp3', 'm4a', 'aac', 'wav', 'flac', 'ogg', 'opus',
]
const MEDIA_ACCEPT = [...MEDIA_SUFFIXES.map((suffix) => `.${suffix}`), 'audio/*', 'video/*'].join(',')
const MEDIA_HINT = 'mp4 / mov / mkv / webm / avi / mp3 / m4a / wav / flac / ogg / opus / aac'
/** 批量本地文件的上限，与后端 /api/job-batches/upload 保持一致。 */
const MAX_BATCH_FILES = 50

function fileSizeLabel(bytes: number) {
  return bytes < 1024 ** 2 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : sizeLabel(bytes)
}

/** 判断两个 File 是不是同一个文件：同名同大小同修改时间视为重复，主要用于拖拽去重。 */
function fileKey(file: File) {
  return `${file.name}:${file.size}:${file.lastModified}`
}

/** 拖进来的目录（没有后缀）与其它非媒体文件都会被这一步挡掉。 */
function isSupportedMedia(file: File) {
  const index = file.name.lastIndexOf('.')
  const suffix = index < 0 ? '' : file.name.slice(index + 1).toLowerCase()
  return MEDIA_SUFFIXES.includes(suffix)
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
  // 批量模式有两种来源：链接（逐行预检）与本地文件（选好后一次上传成一个批次）
  const [batchSourceKind, setBatchSourceKind] = useState<'links' | 'files'>('links')
  const [batchFiles, setBatchFiles] = useState<File[]>([])
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
  // 最近文案 / 任务结果点击 → 文案弹窗（阅读或编辑）
  const [docOverlay, setDocOverlay] = useState<{ id: string; mode: OverlayMode } | null>(null)
  // 本次会话刚提交的任务：刷新页面后清空，任务执行面板不会回显历史任务
  const [focusedJobId, setFocusedJobId] = useState('')
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [uploading, setUploading] = useState(false)
  // 多文件串行提交时的进度文案（「正在导入 2/3」）
  const [uploadLabel, setUploadLabel] = useState('')
  // 拖入本地文件时的全屏提示：dragover 期间持续为 true
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [createdBatchId, setCreatedBatchId] = useState('')
  // 服务端最近批次的摘要：右栏要靠它认出「正在跑的那个批次」，不能只认本地刚提交的 id
  const [recentBatches, setRecentBatches] = useState<JobBatch[]>([])
  // 当前展示的批次详情：批量执行时右栏要逐条显示子任务，只有 id 看不到每条的状态
  const [batchDetail, setBatchDetail] = useState<JobBatchDetail | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const sourceInput = useRef<HTMLInputElement>(null)
  const batchInput = useRef<HTMLTextAreaElement>(null)
  // 拖放监听只绑一次（绑在 window 上），用 ref 取到最新的导入函数，
  // 否则闭包里会一直拿到首次渲染时的模式与模型选择。
  const importFilesRef = useRef<(files: File[]) => Promise<void>>(async () => undefined)

  // 侧栏「新建提取」的落点：已经在工作台时不再是一次「回到当前页」的空导航，
  // 而是把表单复位到可以直接输入的状态。
  useEffect(() => {
    function reset() {
      setCaptureMode('single')
      setSource('')
      setBatchSource('')
      setBatchPreflight(null)
      setPreflightError('')
      setError('')
      setNotice('')
      window.scrollTo({ top: 0, behavior: 'smooth' })
      sourceInput.current?.focus()
    }
    window.addEventListener('vtw:new-task', reset)
    return () => window.removeEventListener('vtw:new-task', reset)
  }, [])

  /**
   * 拖入本地音视频即可导入，与「导入本地文件」按钮共用 importFiles。
   *
   * 监听绑在 window 上而不是某个容器：拖到页面任意位置（包括侧栏）松手都能导入，
   * 不用先把文件瞄到那个按钮上。遮罩用 `pointer-events: none`，事件照旧落在这里。
   */
  useEffect(() => {
    let hideTimer = 0
    // 拖选中的文字也会触发 drag 事件，只有带 Files 的才接管
    function hasFiles(event: DragEvent) {
      return Array.from(event.dataTransfer?.types ?? []).includes('Files')
    }
    function onDragOver(event: DragEvent) {
      if (!hasFiles(event)) return
      // 必须阻止默认行为，否则浏览器会直接打开拖进来的文件
      event.preventDefault()
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
      setDragging(true)
      // 拖动期间 dragover 会持续触发；停 200ms 说明已经离开或取消了这次拖拽
      window.clearTimeout(hideTimer)
      hideTimer = window.setTimeout(() => setDragging(false), 200)
    }
    function onDrop(event: DragEvent) {
      if (!hasFiles(event)) return
      event.preventDefault()
      window.clearTimeout(hideTimer)
      setDragging(false)
      void importFilesRef.current(Array.from(event.dataTransfer?.files ?? []))
    }
    window.addEventListener('dragover', onDragOver)
    window.addEventListener('drop', onDrop)
    return () => {
      window.clearTimeout(hideTimer)
      window.removeEventListener('dragover', onDragOver)
      window.removeEventListener('drop', onDrop)
    }
  }, [])

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
    if (captureMode !== 'batch' || batchSourceKind !== 'links') return
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
  }, [batchSource, batchSourceKind, captureMode])

  function selectCaptureMode(next: 'single' | 'batch') {
    setCaptureMode(next)
    setBatchPreflight(null)
    setPreflightError('')
    setPreflighting(false)
  }

  function selectBatchSourceKind(next: 'links' | 'files') {
    setBatchSourceKind(next)
    setBatchPreflight(null)
    setPreflightError('')
    setPreflighting(false)
    setError('')
    setNotice('')
  }

  function changeBatchSource(value: string) {
    setBatchSource(value)
    setBatchPreflight(null)
    setPreflightError('')
    setPreflighting(false)
  }

  /** 把光标定位到批量输入框里出问题的那一行，省去用户自己数行号。 */
  function focusBatchLine(position: number) {
    const node = batchInput.current
    if (!node) return
    const lines = batchSource.split(/\r?\n/)
    let start = 0
    for (let index = 0; index < position - 1 && index < lines.length; index += 1) {
      start += lines[index].length + 1
    }
    node.focus()
    node.setSelectionRange(start, start + (lines[position - 1]?.length ?? 0))
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
        const installed = recognitionModels(nextCatalog?.models).filter((item) => item.state === 'ready')
        const pick = installed.find((item) => item.id === preferred)
          ?? installed.find((item) => item.recommended)
          ?? installed[0]
        setModelId(pick?.id ?? '')
      })
    return () => { active = false }
  }, [])

  // 只列识别模型：标点恢复与语音合成也在这个目录里，但它们不能用来转写
  const models = recognitionModels(catalog?.models)
  const missingModels = models.filter((item) => item.state !== 'ready')
  const readyModels = models.filter((item) => item.state === 'ready')
  const selectedModel = models.find((item) => item.id === modelId)
  const selectedModelName = selectedModel ? shortModelName(selectedModel.name) : ''
  /** 把「平台字幕开关 + 所选模型」的组合结果讲成一句话，用户不用自己推演 */
  const planSummary = preferSubtitle
    ? selectedModelName
      ? `有平台字幕就先读字幕，没有则用 ${selectedModelName} 识别`
      : '有平台字幕就先读字幕；没有字幕时需要先下载识别模型'
    : selectedModelName
      ? `跳过平台字幕，全部用 ${selectedModelName} 重新识别（带标点与分段）`
      : '已关闭平台字幕，且没有可用模型：先下载模型，或重新打开平台字幕'

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
    if (
      captureMode === 'batch'
      && batchSourceKind === 'links'
      && (!batchPreflight?.can_submit || sources.length === 0)
    ) return
    if (captureMode === 'batch' && batchSourceKind === 'files' && batchFiles.length === 0) return
    setSubmitting(true)
    setError('')
    setNotice('')
    setCreatedBatchId('')
    setBatchDetail(null)
    try {
      if (captureMode === 'batch') {
        if (batchSourceKind === 'files') {
          const created = await api.uploadJobBatch(batchFiles, submitMode(), modelId || null, preferSubtitle)
          setJobs((current) => [...created.jobs, ...current].slice(0, 4))
          setFocusedJobId(created.jobs[0]?.id ?? '')
          setCreatedBatchId(created.id)
          setBatchDetail(created)
          setBatchFiles([])
          setNotice(`已创建文件批次，共 ${created.total_count} 个文件。`)
          return
        }
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

  function upload(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    event.target.value = ''
    void importFiles(files)
  }

  /** 把文件加进批量待上传列表：过滤格式、按「文件名 + 大小 + 修改时间」去重、封顶 50 个。 */
  function addBatchFiles(files: File[]) {
    const accepted = files.filter(isSupportedMedia)
    const skipped = files.length - accepted.length
    setError('')
    setNotice('')
    if (!accepted.length) {
      setError(`不支持这些文件的格式（支持 ${MEDIA_HINT}）`)
      return
    }
    const seen = new Set(batchFiles.map(fileKey))
    const unique = accepted.filter((file) => {
      const key = fileKey(file)
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    const room = Math.max(0, MAX_BATCH_FILES - batchFiles.length)
    const added = unique.slice(0, room)
    if (added.length) setBatchFiles((current) => [...current, ...added])
    if (skipped > 0) setNotice(`跳过 ${skipped} 个不支持的格式。`)
    else if (unique.length > added.length) setNotice(`单个批次最多 ${MAX_BATCH_FILES} 个文件，多出的已忽略。`)
  }

  function removeBatchFile(index: number) {
    setBatchFiles((current) => current.filter((_, position) => position !== index))
  }

  /**
   * 导入本地文件：按钮选文件与拖进来走的是同一条路径。
   *
   * 一次拖多个文件时串行提交（后端一次只收一个文件），某个失败不影响其余几个；
   * 结果显示成一句汇总，而不是让最后一个文件的状态盖掉前面所有结果。
   */
  async function importFiles(files: File[]) {
    if (!files.length) return
    // 批量模式下选择/拖入文件不再立即上传，而是先进入待上传列表：
    // 用户可以确认文件名与数量后，再一次性提交成一个批次。
    if (captureMode === 'batch') {
      selectBatchSourceKind('files')
      addBatchFiles(files)
      return
    }
    const accepted = files.filter(isSupportedMedia)
    const skipped = files.length - accepted.length
    if (!accepted.length) {
      setNotice('')
      setError(`不支持这些文件的格式（支持 ${MEDIA_HINT}）`)
      return
    }

    setUploading(true)
    setError('')
    setNotice('')
    const created: Job[] = []
    let failed = 0
    try {
      for (const [index, file] of accepted.entries()) {
        setUploadLabel(accepted.length > 1 ? `正在导入 ${index + 1}/${accepted.length}` : '正在导入')
        try {
          created.push(await api.uploadJob(file, submitMode(), modelId || null, preferSubtitle))
        } catch {
          failed += 1
        }
      }
      if (created.length) {
        setJobs((current) => [...created, ...current].slice(0, 4))
        setFocusedJobId(created[0].id)
      }
      if (created.length === 1 && !skipped && !failed) {
        setNotice(`“${accepted[0].name}”已加入队列。`)
        return
      }
      const parts: string[] = []
      if (created.length) parts.push(`已导入 ${created.length} 个文件`)
      if (skipped) parts.push(`跳过 ${skipped} 个不支持的格式`)
      if (failed) parts.push(`${failed} 个导入失败`)
      if (created.length) setNotice(`${parts.join('，')}。`)
      else setError(`${parts.join('，') || '导入失败'}，请确认文件可用`)
    } finally {
      setUploading(false)
      setUploadLabel('')
    }
  }

  // 每渲染一次就把最新的导入函数挂到 ref 上：上面的拖放监听只绑一次，
  // 不能让它闭包捕获某个渲染的 importFiles（否则永远用首次渲染的模式与模型）。
  useEffect(() => {
    importFilesRef.current = importFiles
  })

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
          {/* 隐私承诺已经出现在右侧标记与左栏页脚两处，这里只说下一步做什么 */}
          <p>粘贴链接或导入本地文件，提取完成后去文案库校对、导出。</p>
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
        <div className="segmented-switch" role="radiogroup" aria-label="提取方式">
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
                  ref={sourceInput}
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
              <div className="batch-source-kind" role="radiogroup" aria-label="批量来源">
                <button
                  type="button"
                  role="radio"
                  aria-checked={batchSourceKind === 'links'}
                  className={batchSourceKind === 'links' ? 'active' : ''}
                  onClick={() => selectBatchSourceKind('links')}
                >
                  <Link2 size={15} /> 链接
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={batchSourceKind === 'files'}
                  className={batchSourceKind === 'files' ? 'active' : ''}
                  onClick={() => selectBatchSourceKind('files')}
                >
                  <FileUp size={15} /> 本地文件
                </button>
              </div>

              {batchSourceKind === 'links' ? (
                <>
                  <label htmlFor="batch-source">每行粘贴一条视频链接或分享文案</label>
                  <textarea
                    id="batch-source"
                    ref={batchInput}
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
                      <p><TriangleAlert size={15} /> 先修正这几条，点一行可跳到对应输入行</p>
                      {/* 之前只把「第 N 行」当文字列出来，用户得自己回去数行号；
                          现在每条都能点，点完光标就落在那一行上 */}
                      <ul className="batch-issue-list">
                        {batchPreflight.items.filter((item) => item.status !== 'valid').map((item) => (
                          <li key={`${item.position}-${item.status}`}>
                            <button type="button" onClick={() => focusBatchLine(item.position)}>
                              <span className="batch-issue-line">第 {item.position} 行</span>
                              <span>{item.message}</span>
                            </button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {batchLines(batchSource).length > 50 && (
                    <p className="batch-preflight issues" role="alert"><TriangleAlert size={15} /> 单个批次最多 50 条，请拆成多个批次提交。</p>
                  )}
                  {preflightError && <p className="batch-preflight issues" role="alert"><TriangleAlert size={15} /> {preflightError}</p>}
                </>
              ) : (
                <div className="batch-files">
                  <div className="batch-file-drop">
                    <FileUp size={22} />
                    <strong>选择或拖入本地音视频</strong>
                    <small>支持 {MEDIA_HINT}，一次最多 {MAX_BATCH_FILES} 个</small>
                    <button
                      className="file-button"
                      type="button"
                      onClick={() => fileInput.current?.click()}
                      disabled={submitting || uploading}
                    >
                      <FileUp size={16} /> 选择文件
                    </button>
                  </div>
                  {batchFiles.length > 0 && (
                    <ul className="batch-file-list">
                      {batchFiles.map((file, index) => (
                        <li key={`${fileKey(file)}:${index}`}>
                          <span className="batch-file-name" title={file.name}>{file.name}</span>
                          <span className="batch-file-size">{fileSizeLabel(file.size)}</span>
                          <button
                            type="button"
                            aria-label={`移除 ${file.name}`}
                            onClick={() => removeBatchFile(index)}
                            disabled={submitting}
                          >
                            <X size={14} />
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="batch-submit-row">
                    <span>{batchFiles.length}/{MAX_BATCH_FILES} 个文件</span>
                    <button
                      className="primary-button"
                      type="submit"
                      disabled={submitting || batchFiles.length === 0}
                    >
                      {submitting ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}
                      {submitting ? '正在创建批次' : `开始批量导入${batchFiles.length ? `（${batchFiles.length}）` : ''}`}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* 识别设置：模型、本地导入、平台字幕原来散在三处、权重也不一样，
              但它们回答的是同一个问题——「这段内容怎么变成文字」。
              收进一块之后，标题右侧直接给出当前实际会怎么跑。 */}
          <div className="capture-settings">
            <div className="capture-settings-head">
              <strong>识别设置</strong>
              <small>{planSummary}</small>
            </div>
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
                accept={MEDIA_ACCEPT}
                multiple
                onChange={upload}
              />
              <button
                className="file-button"
                type="button"
                onClick={() => fileInput.current?.click()}
                disabled={uploading || submitting}
                title={captureMode === 'batch'
                  ? '把文件加入上方的批量待上传列表'
                  : '选文件，或把音视频文件直接拖进这一页'}
              >
                {uploading ? <LoaderCircle className="spin" size={16} /> : <FileUp size={16} />}
                {uploading
                  ? (uploadLabel || '正在导入')
                  : captureMode === 'batch' ? '添加本地文件' : '导入本地文件'}
              </button>
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
          </div>
          {error && <p className="form-message error" role="alert">{error}</p>}
          {notice && (
            <p className="form-message success" role="status">
              {notice} {createdBatchId && <Link to={`/jobs?batch=${createdBatchId}`}>查看批次</Link>}
            </p>
          )}
        </form>
      </section>

        </div>

        {/* 右栏＝「现在在跑什么」＋「刚拿到什么」。
            原来「最近文案」排在左列表单下面，右栏只有一小块进度卡、整片留白；
            这两件事本来就都回答「任务的结果」，收到同一列更顺，也让左列只专注一件事：提交。
            整列吸顶（见 styles.css 的 .workbench-aside），长列表不会把进度条滚出视口。 */}
        <div className="workbench-aside">
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
                onOpenDocument={(id) => setDocOverlay({ id, mode: 'read' })}
              />
            ) : current ? (
              <TaskExecution
                job={current}
                title={jobDisplayTitle(current, titles)}
                onOpenDocument={(id) => setDocOverlay({ id, mode: 'read' })}
                onRetry={retryJob}
              />
            ) : (
              <div className="compact-empty"><span>没有正在执行的任务</span><p>提交提取后，这里会实时显示每一步进度。</p></div>
            )}
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
                  // 行本身不再是整块链接：里面既有「看」又有「改」两个动作，
                  // 把按钮塞进 <a> 里是非法嵌套，键盘与读屏都会读到矛盾的角色
                  <div className="document-row" key={document.id}>
                    <Thumb src={document.has_cover ? api.coverUrl(document.id) : null} />
                    <span className="document-info">
                      {/* 标题是进编辑页的正路；要看内容不必跳页，用行尾的「预览」 */}
                      <strong title={document.title}>
                        <Link
                          className="document-title"
                          to={`/documents/${document.id}`}
                          state={{ from: '/', label: '工作台' }}
                          onClick={(event) => {
                            // 左键在弹窗里改；Ctrl / Cmd / Shift+点击仍可开整页
                            if (event.metaKey || event.ctrlKey || event.shiftKey) return
                            event.preventDefault()
                            setDocOverlay({ id: document.id, mode: 'edit' })
                          }}
                        >
                          {document.title}
                        </Link>
                      </strong>
                      <small>{timeLabel(document.updated_at)} · {document.word_count} 字</small>
                    </span>
                    <PlatformBadge platform={document.platform} />
                    <span className="document-actions">
                      <button type="button" onClick={() => setDocOverlay({ id: document.id, mode: 'read' })}>
                        <Eye size={14} /> 预览
                      </button>
                    </span>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      </div>
      {/* 拖入提示：整窗盖一层，松开即导入；样式里 pointer-events: none，不抢 drop 事件 */}
      {dragging && (
        <div className="dropzone-overlay" aria-hidden="true">
          <div className="dropzone-card">
            <FileUp size={26} />
            <strong>松开即可导入本地音视频</strong>
            <small>支持 {MEDIA_HINT}</small>
            {captureMode === 'batch' && (
              <small className="dropzone-warn">批量模式下会加入待上传列表，确认后再提交</small>
            )}
          </div>
        </div>
      )}
      {docOverlay && (
        <DocumentOverlay
          key={docOverlay.id}
          documentId={docOverlay.id}
          initialMode={docOverlay.mode}
          // 右栏每 1.6 秒轮询一次，关掉弹窗后自然会刷新，不用额外触发
          onClose={() => setDocOverlay(null)}
        />
      )}
    </div>
  )
}
