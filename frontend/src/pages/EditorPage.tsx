import { ArrowLeft, Bot, Check, Copy, Download, Film, Info, RefreshCw, RotateCcw, Sparkles, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'

import { LoadingState } from '../components/LoadingState'
import { AudioTrackPlayer } from '../components/AudioTrackPlayer'
import { Modal, ModalHeader } from '../components/Modal'
import { PlatformBadge } from '../components/PlatformBadge'
import { api } from '../lib/api'
import { useAiSummary } from '../lib/ai'
import { useConfirm } from '../lib/useConfirm'
import { describeSummaryOutcome } from '../lib/deepseek'
import { transcriptAsText } from '../lib/textFormatting'
import type { DocumentDetail, EditorNavState, TranscriptSegment } from '../types'

function timecode(milliseconds: number | null) {
  if (milliseconds === null) return '--:--'
  const seconds = Math.floor(milliseconds / 1000)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remain = seconds % 60
  return hours ? `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${remain.toString().padStart(2, '0')}` : `${minutes.toString().padStart(2, '0')}:${remain.toString().padStart(2, '0')}`
}

function duration(seconds: number | null) {
  if (!seconds) return '未知'
  const total = Math.round(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const remain = total % 60
  return hours
    ? `${hours} 小时 ${minutes} 分`
    : `${minutes} 分 ${remain} 秒`
}

function decodeEntities(value: string) {
  const named: Record<string, string> = { amp: '&', quot: '"', apos: "'", lt: '<', gt: '>' }
  return value.replace(/&(#x[0-9a-f]+|#\d+|amp|quot|apos|lt|gt);/gi, (match, entity: string) => {
    if (entity[0] !== '#') return named[entity.toLowerCase()] ?? match
    const hexadecimal = entity[1]?.toLowerCase() === 'x'
    const codePoint = Number.parseInt(entity.slice(hexadecimal ? 2 : 1), hexadecimal ? 16 : 10)
    return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : match
  })
}

type TextView = 'timeline' | 'plain'

export interface DocumentEditorProps {
  documentId: string
  /** 整页模式下的返回落点；弹窗模式（传了 onClose）不用 */
  backTo?: string
  backLabel?: string
  /** 传了就是弹窗模式：左侧的返回换成关闭，不再做页面导航 */
  onClose?: () => void
  /** 有未落盘的改动时为 true：弹窗据此决定关闭前要不要确认 */
  onDirtyChange?: (dirty: boolean) => void
  /** 任意一次内容落库后触发：列表据此只刷新真正改过的行，避免空手关闭也重拉一遍 */
  onSaved?: () => void
}

/**
 * 编辑页的整页入口。弹窗模式复用同一个 DocumentEditor，见 DocumentOverlay。
 */
export function EditorPage() {
  const { id = '' } = useParams()
  const location = useLocation()
  const nav = (location.state ?? null) as EditorNavState | null
  // 返回按钮回到进入编辑页的那一页；直接打开链接时没有来源，退回文案库
  return (
    <DocumentEditor
      documentId={id}
      backTo={nav?.from ?? '/library'}
      backLabel={nav?.label ?? '文案库'}
    />
  )
}

/**
 * 文案编辑器：整页与弹窗共用。
 *
 * 两种外壳只差工具栏左侧那一下：整页是「返回上一页」，弹窗是「关闭」——
 * 列表页始终挂在弹窗后面，关掉就回到原来的滚动位置，不需要再导航一次。
 */
export function DocumentEditor({
  documentId,
  backTo = '/library',
  backLabel = '文案库',
  onClose,
  onDirtyChange,
  onSaved,
}: DocumentEditorProps) {
  const navigate = useNavigate()
  const confirm = useConfirm()
  // AI 总结的分流与结果弹窗：驱动从设置读，跳网页还是用本机账号由它决定
  const aiSummary = useAiSummary()
  // 窄屏下来源面板默认收起；宽屏时这个状态不起作用（面板常驻）
  const [sourceOpen, setSourceOpen] = useState(false)
  const [document, setDocument] = useState<DocumentDetail | null>(null)
  const [loadError, setLoadError] = useState('')
  // 加载失败后点「重试」递增，重新拉取文档
  const [reloadKey, setReloadKey] = useState(0)
  const [actionError, setActionError] = useState('')
  const [actionMessage, setActionMessage] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(true)
  const [exporting, setExporting] = useState('')
  // 窄屏把右栏藏起来后，导出与复制全文收进这个弹窗，别让核心产出随布局一起消失
  const [exportOpen, setExportOpen] = useState(false)
  const [copiedSource, setCopiedSource] = useState(false)
  const [formatting, setFormatting] = useState(false)
  const [restoring, setRestoring] = useState(false)
  // 播放中的分段：音轨播放时高亮当前句，点击时间码可跳到该句
  const [activeSegmentId, setActiveSegmentId] = useState<number | null>(null)
  const audioRef = useRef<HTMLAudioElement>(null)
  // 拖动音轨进度时，时间轴框跟着跳，但只跳框内的滚动，不碰页面
  const scrubbingRef = useRef(false)
  // 默认打开全文阅读，需要按时间定位或改字时再切到时间轴
  const [view, setView] = useState<TextView>('plain')
  // 整篇草稿：整篇一个框，改动先留在本地，保存/切视图时才落回分段，
  // 否则打字过程中分段与时间轴会被反复重排。
  const [wholeDraft, setWholeDraft] = useState<string | null>(null)
  const wordCount = useMemo(() => document?.segments.reduce((count, segment) => count + segment.text.trim().length, 0) ?? 0, [document])
  const description = useMemo(() => decodeEntities(document?.description ?? ''), [document?.description])
  /** 整篇文本：与「复制全文」、导出同一份（一行一句；整理过段落之间空行）。 */
  const wholeText = useMemo(
    () => (document ? transcriptAsText(document.segments, document.paragraph_body) : ''),
    [document],
  )
  // 当前编辑框里的内容：保存请求回来时用它判断「这期间有没有新输入」
  const currentTextRef = useRef('')
  // 用 effect 同步而不是在渲染期间直接赋值：渲染期间写 ref 在并发渲染下可能
  // 被丢弃一次，读到的还是上一轮的值，React 的 lint 也会直接拦下来。
  // 读取都发生在异步保存的回调里（晚于本次提交），所以时序上等价。
  useEffect(() => {
    currentTextRef.current = wholeDraft ?? wholeText
  }, [wholeDraft, wholeText])

  // 打开弹窗或整页时都按 documentId 拉一次详情
  useEffect(() => {
    let active = true
    api.document(documentId)
      .then((next) => {
        if (active) setDocument(next)
      })
      .catch((reason: Error) => {
        if (active) setLoadError(reason.message)
      })
    return () => {
      active = false
    }
  }, [documentId, reloadKey])

  // 把「有没有未落盘的改动」告诉弹窗外壳：关闭按钮、Esc、点遮罩都走同一个守卫
  const dirtyNotify = useRef(onDirtyChange)
  useEffect(() => {
    dirtyNotify.current = onDirtyChange
  })
  useEffect(() => {
    dirtyNotify.current?.(!saved)
  }, [saved])

  // 与 dirty 一样走 ref：保存回调每次渲染都是新函数，effect 不该跟着重装
  const savedNotify = useRef(onSaved)
  useEffect(() => {
    savedNotify.current = onSaved
  })

  useEffect(() => {
    if (saved) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [saved])

  /** 把编辑框里的内容落库：整篇正文一行一条写回分段，再顺手保存标题。 */
  async function persistBody(
    current: DocumentDetail,
    title: string,
    text: string,
  ): Promise<DocumentDetail> {
    const updated = await api.saveBody(current.id, text)
    const summary = await api.saveDocument(current.id, { title })
    return { ...updated, ...summary, title }
  }

  /**
   * 落盘当前内容。
   *
   * 草稿不清掉：实时保存发生在一个字刚敲完的瞬间，用服务端回传的文本替换编辑框
   * 会把光标顶到末尾、正在输入的半行也会被重排。谁把整篇换掉了（智能分句、恢复、
   * 切视图）谁负责 setWholeDraft(null)。
   *
   * 保存请求进行中又敲了字时，sent 已经不是最新内容：这时保持「未保存」，
   * 让自动保存再跑一轮，而不是把新输入当成已落盘丢掉。
   */
  async function persistDocument() {
    if (!document) return null
    const title = document.title.trim()
    if (!title) throw new Error('文案标题不能为空')
    const text = wholeDraft ?? wholeText
    const merged = await persistBody(document, title, text)
    setDocument(merged)
    setSaved(currentTextRef.current === text)
    savedNotify.current?.()
    return merged
  }

  async function save(options?: { silent?: boolean }) {
    setSaving(true)
    setActionError('')
    if (!options?.silent) setActionMessage('')
    try {
      await persistDocument()
      if (!options?.silent) setActionMessage('更改已保存到本机')
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '保存失败，请稍后重试')
    } finally {
      setSaving(false)
    }
  }

  /** 当前编辑内容的纯文本：编辑框里有未保存的改动就用编辑框，否则用已落库的分段。 */
  function currentPlainText(): string {
    return wholeDraft ?? wholeText
  }

  async function copyPlainText() {
    if (!document) return
    setActionError('')
    setActionMessage('')
    try {
      await navigator.clipboard.writeText(currentPlainText())
      setActionMessage('已复制当前编辑内容')
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '复制失败，请检查剪贴板权限')
    }
  }

  /**
   * AI 总结：把文案与指令带到 DeepSeek 网页（见 lib/deepseek），本机不做推理。
   */
  async function summarize() {
    if (!document) return
    setActionError('')
    setActionMessage('')
    const result = await aiSummary.start(document.title, currentPlainText())
    const { message, isError } = describeSummaryOutcome(result.outcome)
    if (isError) setActionError(message)
    else setActionMessage(message)
  }

  async function download(format: 'txt' | 'srt' | 'vtt' | 'json') {
    if (!document) return
    setExporting(format)
    setActionError('')
    setActionMessage('')
    try {
      const current = await persistDocument()
      if (!current) return
      const response = await fetch(api.exportUrl(current.id, format))
      if (!response.ok) throw new Error(`导出失败（${response.status}）`)
      const blobUrl = URL.createObjectURL(await response.blob())
      const anchor = window.document.createElement('a')
      anchor.href = blobUrl
      anchor.download = `${current.title}.${format}`
      anchor.click()
      URL.revokeObjectURL(blobUrl)
      setActionMessage(`已导出 ${format.toUpperCase()}`)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '导出失败，请稍后重试')
    } finally {
      setExporting('')
    }
  }

  async function copySource() {
    if (!document) return
    setActionError('')
    setActionMessage('')
    try {
      await navigator.clipboard.writeText(document.source_value)
      setCopiedSource(true)
      window.setTimeout(() => setCopiedSource(false), 2000)
    } catch {
      setActionError('复制失败，请检查剪贴板权限')
    }
  }

  // 编辑区里输入、剪切、撤销都由浏览器原生处理；这里只接住 Ctrl / Cmd + S
  // 做一次立即保存（实时保存之外的手动兜底）。用 ref 保存最新的 save，
  // 避免每次渲染都重新绑定键盘监听。
  const saveRef = useRef(save)
  useEffect(() => {
    saveRef.current = save
  })

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault()
        void saveRef.current()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // 实时保存：停手约 0.6 秒就静默落盘，和普通编辑器一样不用手动保存
  useEffect(() => {
    if (saved || saving) return
    const timer = window.setTimeout(() => void saveRef.current({ silent: true }), 600)
    return () => window.clearTimeout(timer)
  }, [saved, saving])

  /**
   * 智能分句：把文案整理成带标点的自然段，一次请求直接落库生效。
   *
   * 不分来源，规则一致：合并逐字滚动的重复条目，正文没有标点时用本地标点模型补标点，
   * 再切成句子、按句数/字数组织成自然段（一段一条）。整理后当前文案立即换成新的分段。
   */
  async function polishText() {
    if (!document) return
    setFormatting(true)
    setActionError('')
    setActionMessage('')
    try {
      // 先落盘现有改动，避免整理结果被旧草稿覆盖
      await persistDocument()
      const result = await api.polish(document.id)
      setDocument(result.document)
      setWholeDraft(null)
      setSaved(true)
      savedNotify.current?.()
      const notes = [`已整理为 ${result.paragraphs} 个自然段、${result.sentences} 句`]
      if (result.merged > 0) notes.push(`合并了 ${result.merged} 条滚动重复内容`)
      if (!result.punctuated) notes.push('正文已带标点，未再补标点')
      setActionMessage(notes.join('，'))
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '智能分句失败，请稍后重试')
    } finally {
      setFormatting(false)
    }
  }

  /** 恢复原始分段：整理是结构性改动，用识别原文退回「一行一句」，让用户能重来一次。 */
  async function restoreLines() {
    if (!document) return
    const confirmed = await confirm({
      title: '恢复为原始分段？',
      description: '将用识别原文重建分段，当前正文会被替换；识别原文不受影响。',
      confirmLabel: '恢复原始分段',
      tone: 'danger',
    })
    if (!confirmed) return
    setRestoring(true)
    setActionError('')
    setActionMessage('')
    try {
      const updated = await api.restoreLines(document.id)
      setDocument(updated)
      setWholeDraft(null)
      setSaved(true)
      savedNotify.current?.()
      setActionMessage(`已恢复为 ${updated.segments.length} 行原始分段`)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '恢复失败，请稍后重试')
    } finally {
      setRestoring(false)
    }
  }

  /** 恢复这一段的识别原文；只有改过的段落才显示这个入口。 */
  function revertSegment(segment: TranscriptSegment) {
    if (!document) return
    setDocument({
      ...document,
      segments: document.segments.map((item) =>
        item.id === segment.id ? { ...item, text: item.raw_text } : item,
      ),
    })
    setSaved(false)
  }

  /** 播放中每帧更新高亮：找到当前时间落在哪一段。 */
  function onAudioTimeUpdate() {
    const audio = audioRef.current
    if (!audio || !document) return
    const milliseconds = audio.currentTime * 1000
    const current = document.segments.find(
      (segment) =>
        segment.start_ms !== null &&
        segment.end_ms !== null &&
        milliseconds >= segment.start_ms &&
        milliseconds < segment.end_ms,
    )
    setActiveSegmentId(current?.id ?? null)
  }

  /** 点击时间码跳到这句并开始播放。 */
  function seek(segment: TranscriptSegment) {
    const audio = audioRef.current
    if (!audio || segment.start_ms === null) return
    audio.currentTime = segment.start_ms / 1000
    void audio.play()
  }

  /**
   * 把当前句滚进时间轴框的视野。
   *
   * 只改框自己的 scrollTop，不用 `scrollIntoView`：后者会顺带滚动所有可滚动祖先，
   * 页面依旧会被拽走。这里按「框的上下边」算一个最小位移，效果等同 `block: nearest`，
   * 但只作用于时间轴框。拖进度条时用瞬间跳转，正常播放时用平滑滚动。
   */
  useEffect(() => {
    if (activeSegmentId === null || view !== 'timeline') return
    if (window.document.activeElement instanceof HTMLTextAreaElement) return
    const row = window.document.querySelector(`[data-segment-id="${activeSegmentId}"]`)
    const panel = row?.closest<HTMLElement>('.segment-panel')
    if (!row || !panel) return
    const margin = 12
    const rowBox = row.getBoundingClientRect()
    const panelBox = panel.getBoundingClientRect()
    let delta = 0
    if (rowBox.top < panelBox.top + margin) {
      delta = rowBox.top - panelBox.top - margin
    } else if (
      rowBox.bottom > panelBox.bottom - margin &&
      rowBox.height < panelBox.height - margin * 2
    ) {
      delta = rowBox.bottom - panelBox.bottom + margin
    }
    if (delta !== 0) {
      panel.scrollBy({
        top: delta,
        // 拖动进度条、或跳转跨度超过一屏（点时间码/大跨度 seek）时直接落位；
        // 正常播放时一次只挪一行，平滑滚动更跟眼。
        behavior:
          scrubbingRef.current || Math.abs(delta) > panel.clientHeight ? 'auto' : 'smooth',
      })
    }
  }, [activeSegmentId, view])

  /** 切换视图前先把未保存的改动存下来，再收起草稿（时间轴直接看分段）。 */
  async function switchView(next: TextView) {
    if (!saved) {
      try {
        await persistDocument()
      } catch (reason) {
        setActionError(reason instanceof Error ? reason.message : '保存失败，请稍后重试')
        return
      }
    }
    setWholeDraft(null)
    setView(next)
  }

  /**
   * 整页模式下带着未保存的改动离开时先问一句。改成自绘弹窗后不能再同步返回，
   * 所以先拦住默认跳转，等到用户确认了再自己 navigate 过去。
   */
  function confirmLeave(event: MouseEvent<HTMLAnchorElement>) {
    if (saved) return
    event.preventDefault()
    void confirm({
      title: '还有未保存的更改',
      description: '现在离开会丢掉刚才的修改；识别原文与已经保存过的内容不受影响。',
      confirmLabel: '离开这一页',
      cancelLabel: '留下继续编辑',
      tone: 'danger',
    }).then((ok) => {
      if (ok) navigate(backTo)
    })
  }

  if (loadError) {
    return (
      <div className="page editor-load-error">
        <p className="form-message error" role="alert">{loadError}</p>
        <div className="error-actions">
          <button className="secondary-button" type="button" onClick={() => { setLoadError(''); setReloadKey((key) => key + 1) }}>
            <RefreshCw size={15} /> 重试
          </button>
          {onClose
            ? <button className="text-button" type="button" onClick={onClose}>关闭</button>
            : <Link className="text-button" to={backTo}>返回{backLabel}</Link>}
        </div>
      </div>
    )
  }
  // 与其它页一致：加载期间给骨架，而不是一行纯文字
  if (!document) {
    return (
      <div className="page loading-page">
        <LoadingState label="正在打开文案…" rows={5} />
      </div>
    )
  }

  return (
    <div className={`editor-page${view === 'timeline' ? ' timeline-mode' : ''}`}>
      <header className="editor-toolbar">
        {onClose ? (
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            aria-label="关闭编辑，返回列表"
            title="关闭（返回列表）"
          >
            <X size={19} />
          </button>
        ) : (
          <Link className="icon-button" to={backTo} aria-label={`返回${backLabel}`} title={`返回${backLabel}`} onClick={confirmLeave}><ArrowLeft size={19} /></Link>
        )}
        <div className="editor-title-group"><input aria-label="文案标题" title={document.title} value={document.title} onChange={(event) => { setDocument({ ...document, title: event.target.value }); setSaved(false) }} /></div>
        <div className="editor-actions">
          {/* 只在窄屏出现：展开被折叠的来源信息（封面、作者、时长、音轨播放器） */}
          <button
            className="icon-button editor-source-toggle"
            type="button"
            aria-expanded={sourceOpen}
            aria-controls="editor-source"
            title="来源信息"
            aria-label={sourceOpen ? '收起来源信息' : '展开来源信息'}
            onClick={() => setSourceOpen((open) => !open)}
          >
            <Info size={18} />
          </button>
          <button
            className="secondary-button editor-export-button"
            type="button"
            onClick={() => setExportOpen(true)}
            title="导出 TXT、SRT、VTT、JSON，或复制全文"
          >
            <Download size={16} /> 导出
          </button>
          <button className="secondary-button editor-copy-button" type="button" onClick={() => void copyPlainText()}><Copy size={16} /> 复制全文</button>
          <button
            className="secondary-button"
            type="button"
            onClick={() => void summarize()}
            disabled={aiSummary.resolving}
            title="把文案与总结指令带到 DeepSeek 网页，用你自己的账号完成总结"
          >
            <Bot size={16} /> {aiSummary.resolving ? '准备中…' : 'AI 总结'}
          </button>
        </div>
      </header>
      {(actionError || actionMessage) && <div className={`editor-feedback ${actionError ? 'error' : 'success'}`} role={actionError ? 'alert' : 'status'}>{actionError || actionMessage}</div>}
      <div className="editor-workspace">
        <aside
          className={sourceOpen ? 'editor-source-panel open' : 'editor-source-panel'}
          id="editor-source"
          aria-label="来源信息"
        >
          {/* 封面文件可能已被清理：加载失败直接收起，不留破图框 */}
          {document.has_cover && (
            <img
              className="source-cover"
              src={api.coverUrl(document.id)}
              alt=""
              onError={(event) => { event.currentTarget.style.display = 'none' }}
            />
          )}
          <div className="source-details">
            <div className="source-heading">
              <span>来源</span>
              <PlatformBadge platform={document.platform} />
            </div>
            <a href={document.source_type === 'url' ? document.source_value : undefined} target="_blank" rel="noreferrer" title={document.source_value}>{document.source_value}</a>
            <button className="source-copy" type="button" onClick={() => void copySource()}><Copy size={13} /> {copiedSource ? '已复制' : '复制链接'}</button>
            <dl>
              {document.uploader && <div className="source-uploader"><dt>作者</dt><dd title={document.uploader}>{document.uploader}</dd></div>}
              {typeof document.duration_seconds === 'number' && <div className="source-duration"><dt>时长</dt><dd>{duration(document.duration_seconds)}</dd></div>}
              <div><dt>字数</dt><dd>{wordCount}</dd></div>
              <div><dt>分段</dt><dd>{document.segments.length}</dd></div>
            </dl>
          </div>
          {description && (
            <div className="source-description">
              <span>作品介绍</span>
              <p>{description}</p>
            </div>
          )}
          {/* 只有真的保留了音轨才显示播放器；没保留时不占位置，是否保留在「设置」里改 */}
          {document.media_available && (
            <div className="media-panel">
              <span><Film size={13} /> 原始音轨</span>
              <AudioTrackPlayer
                key={document.id}
                src={api.mediaUrl(document.id)}
                audioRef={audioRef}
                onTimeUpdate={onAudioTimeUpdate}
                onScrubStart={() => {
                  scrubbingRef.current = true
                }}
                onScrubEnd={() => {
                  scrubbingRef.current = false
                }}
              />
            </div>
          )}
        </aside>
        <main className={`transcript-editor${view === 'timeline' ? ' timeline-mode' : ''}`} aria-label="文案编辑器">
          <div className="transcript-heading">
            <div><h1>校对文案</h1><span className="editor-subtitle">{view === 'timeline' ? `时间轴 · 共 ${document.segments.length} 段 · ${wordCount} 字` : `全文 · 整篇编辑（${document.segments.length} 段）· ${wordCount} 字`}</span></div>
            <div className="heading-side">
              <div className="view-switch" role="tablist" aria-label="展示方式">
                <button type="button" role="tab" aria-selected={view === 'plain'} className={view === 'plain' ? 'active' : ''} onClick={() => void switchView('plain')}>全文</button>
                <button type="button" role="tab" aria-selected={view === 'timeline'} className={view === 'timeline' ? 'active' : ''} onClick={() => void switchView('timeline')}>时间轴</button>
              </div>
              <span className={`autosave-status${saved ? '' : ' unsaved'}`}><Check size={14} /> {saving ? '保存中…' : saved ? '已自动保存' : '正在保存…'}</span>
            </div>
          </div>
          {/* 「智能分句」会改写整篇正文，是动作而不是视图切换：
              原来和「全文 / 时间轴」两个 tab 挤在同一行，看起来像同一个开关 */}
          <div className="transcript-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={() => void polishText()}
              disabled={formatting || document.segments.length === 0}
              title="把文案整理成带标点的自然段（正文没有标点时需先在设置里安装「中文标点恢复」模型）"
            >
              <Sparkles size={15} /> {formatting ? '整理中…' : '智能分句与标点'}
            </button>
            <span>整理成带标点的自然段并直接生效；不满意可用「恢复原始分段」退回。</span>
          </div>
          {view === 'timeline' ? (
            <div className="segment-panel">
              <div className="segment-list">
                {document.segments.map((segment, index) => {
                  const edited = segment.raw_text !== segment.text
                  return (
                    <div
                      className={`segment-row${activeSegmentId === segment.id ? ' playing' : ''}`}
                      data-segment-id={segment.id}
                      key={segment.id}
                    >
                      {/* 时间码可点：跳到这句开始播放，跟听校对时不用自己拖进度条 */}
                      <button
                        className="timecode"
                        type="button"
                        onClick={() => seek(segment)}
                        disabled={segment.start_ms === null}
                        title={segment.start_ms === null ? undefined : '跳到这句播放'}
                      >
                        {timecode(segment.start_ms)}
                      </button>
                      <span className="segment-node" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
                      <textarea value={segment.text} rows={1} onChange={(event) => { const segments = document.segments.map((item) => item.id === segment.id ? { ...item, text: event.target.value } : item); setDocument({ ...document, segments }); setSaved(false) }} onBlur={() => { if (!saved) void save({ silent: true }) }} aria-label={`第 ${index + 1} 段文案`} />
                      {edited && (
                        <button className="segment-revert" type="button" onClick={() => revertSegment(segment)} title="恢复识别原文" aria-label={`恢复第 ${index + 1} 段的识别原文`}>
                          <RotateCcw size={13} />
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          ) : (
            <div className="fulltext-view">
              {wholeText.trim() === '' ? (
                <p className="compact-empty">还没有内容</p>
              ) : (
                <textarea
                  className="fulltext-paragraph fulltext-whole"
                  value={wholeDraft ?? wholeText}
                  onChange={(event) => {
                    setWholeDraft(event.target.value)
                    setSaved(false)
                  }}
                  // 离开编辑框立即落盘，不用等自动保存的间隔
                  onBlur={() => { if (!saved) void save({ silent: true }) }}
                  aria-label="整篇文案"
                />
              )}
              <p className="fulltext-note">
                {document.paragraph_body
                  ? '「智能分句」整理过：一个自然段就是一个分段，段落之间空行（时间码按字数比例分摊）。改动实时自动保存，Ctrl + S 立即保存；要逐行对照请切到「时间轴」。'
                  : '一行就是一条分段（时间码精确）。点「智能分句」会把整篇整理成带标点的自然段并直接生效；改动实时自动保存，要按时间逐句定位请切到「时间轴」。'}
              </p>
              {document.paragraph_body && (
                <div className="fulltext-actions">
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => void restoreLines()}
                    disabled={restoring}
                  >
                    <RotateCcw size={14} /> {restoring ? '恢复中' : '恢复原始分段'}
                  </button>
                  <span>整理过的分段想退回一行一句，用识别原文重建。</span>
                </div>
              )}
            </div>
          )}
        </main>
        <aside className="editor-inspector" aria-label="文案信息">
          <h2>文案信息</h2>
          <div className="inspector-block"><span>原视频时长</span><strong>{duration(document.duration_seconds)}</strong><small>用于估算校对所需时间</small></div>
          <div className="inspector-block"><span>识别原文</span><strong>已保留</strong><small>导出 JSON 时会附带识别原文，方便对比改动前后</small></div>
          <div className="inspector-block"><span>导出格式</span><div className="format-chips">{(['txt', 'srt', 'vtt', 'json'] as const).map((format) => <button key={format} type="button" onClick={() => void download(format)} disabled={Boolean(exporting)}>{exporting === format ? '…' : format.toUpperCase()}</button>)}</div></div>
        </aside>
      </div>
      {exportOpen && (
        <Modal ariaLabel="导出与复制文案" size="sm" onClose={() => setExportOpen(false)}>
          <ModalHeader
            title="导出与复制"
            icon={Download}
            onClose={() => setExportOpen(false)}
            closeLabel="关闭导出"
          />
          <div className="modal-body editor-export-modal">
            <p>导出前会先保存当前修改，再下载到浏览器的下载目录。</p>
            <div className="editor-export-grid">
              {([
                ['txt', '纯文本'],
                ['srt', '字幕文件'],
                ['vtt', '网页字幕'],
                ['json', '时间码与识别原文'],
              ] as const).map(([format, label]) => (
                <button
                  key={format}
                  className="secondary-button"
                  type="button"
                  disabled={Boolean(exporting)}
                  onClick={() => {
                    setExportOpen(false)
                    void download(format)
                  }}
                >
                  <strong>{exporting === format ? '…' : format.toUpperCase()}</strong>
                  <small>{label}</small>
                </button>
              ))}
            </div>
            <button
              className="secondary-button editor-export-copy"
              type="button"
              onClick={() => {
                setExportOpen(false)
                void copyPlainText()
              }}
            >
              <Copy size={16} /> 复制全文
            </button>
          </div>
        </Modal>
      )}
    </div>
  )
}
