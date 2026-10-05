import { ArrowLeft, Bot, Check, Copy, Film, RefreshCw, RotateCcw, Sparkles } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'

import { PlatformBadge } from '../components/PlatformBadge'
import { api } from '../lib/api'
import { describeSummaryOutcome, openDeepSeekSummary } from '../lib/deepseek'
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

export function EditorPage() {
  const { id = '' } = useParams()
  const location = useLocation()
  const nav = (location.state ?? null) as EditorNavState | null
  // 返回按钮回到进入编辑页的那一页；直接打开链接时没有来源，退回文案库
  const backTo = nav?.from ?? '/library'
  const backLabel = nav?.label ?? '文案库'
  const [document, setDocument] = useState<DocumentDetail | null>(null)
  const [loadError, setLoadError] = useState('')
  // 加载失败后点「重试」递增，重新拉取文档
  const [reloadKey, setReloadKey] = useState(0)
  const [actionError, setActionError] = useState('')
  const [actionMessage, setActionMessage] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(true)
  const [exporting, setExporting] = useState('')
  const [copiedSource, setCopiedSource] = useState(false)
  const [formatting, setFormatting] = useState(false)
  const [restoring, setRestoring] = useState(false)
  // 播放中的分段：音轨播放时高亮当前句，点击时间码可跳到该句
  const [activeSegmentId, setActiveSegmentId] = useState<number | null>(null)
  const audioRef = useRef<HTMLAudioElement>(null)
  // 默认打开全文阅读，需要按时间定位或改字时再切到时间轴
  const [view, setView] = useState<TextView>('plain')
  // 整篇草稿：整篇一个框，改动先留在本地，保存/切视图时才落回分段，
  // 否则打字过程中分段与时间轴会被反复重排。
  const [wholeDraft, setWholeDraft] = useState<string | null>(null)
  const wordCount = useMemo(() => document?.segments.reduce((count, segment) => count + segment.text.trim().length, 0) ?? 0, [document])
  const description = useMemo(() => decodeEntities(document?.description ?? ''), [document?.description])
  /** 整篇文本：与「复制全文」、导出、预览同一份（一行一句；整理过段落之间空行）。 */
  const wholeText = useMemo(
    () => (document ? transcriptAsText(document.segments, document.paragraph_body) : ''),
    [document],
  )
  // 当前编辑框里的内容：保存请求回来时用它判断「这期间有没有新输入」
  const currentTextRef = useRef('')
  currentTextRef.current = wholeDraft ?? wholeText

  useEffect(() => {
    let active = true
    api.document(id)
      .then((next) => {
        if (active) setDocument(next)
      })
      .catch((reason: Error) => {
        if (active) setLoadError(reason.message)
      })
    return () => {
      active = false
    }
  }, [id, reloadKey])

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

  /** AI 总结：把文案与总结指令带到 DeepSeek（预填或剪贴板，见 lib/deepseek）。 */
  async function summarizeWithDeepSeek() {
    if (!document) return
    setActionError('')
    setActionMessage('')
    const { message, isError } = describeSummaryOutcome(
      await openDeepSeekSummary(document.title, currentPlainText()),
    )
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
    const confirmed = window.confirm(
      '将用识别原文重建分段，当前正文会被替换（识别原文不受影响）。继续吗？',
    )
    if (!confirmed) return
    setRestoring(true)
    setActionError('')
    setActionMessage('')
    try {
      const updated = await api.restoreLines(document.id)
      setDocument(updated)
      setWholeDraft(null)
      setSaved(true)
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

  // 播放时把当前句滚进视野；用户正在某个输入框里打字时不打扰
  useEffect(() => {
    if (activeSegmentId === null || view !== 'timeline') return
    if (window.document.activeElement instanceof HTMLTextAreaElement) return
    const row = window.document.querySelector(`[data-segment-id="${activeSegmentId}"]`)
    row?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
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

  function confirmLeave(event: MouseEvent<HTMLAnchorElement>) {
    if (!saved && !window.confirm('还有未保存的更改，确定离开吗？')) event.preventDefault()
  }

  if (loadError) {
    return (
      <div className="page">
        <p className="form-message error" role="alert">{loadError}</p>
        <div className="error-actions">
          <button className="secondary-button" type="button" onClick={() => { setLoadError(''); setReloadKey((key) => key + 1) }}>
            <RefreshCw size={15} /> 重试
          </button>
          <Link className="text-button" to={backTo}>返回{backLabel}</Link>
        </div>
      </div>
    )
  }
  if (!document) return <div className="page loading-page">正在打开文案…</div>

  return (
    <div className="editor-page">
      <header className="editor-toolbar">
        <Link className="icon-button" to={backTo} aria-label={`返回${backLabel}`} title={`返回${backLabel}`} onClick={confirmLeave}><ArrowLeft size={19} /></Link>
        <div className="editor-title-group"><input aria-label="文案标题" title={document.title} value={document.title} onChange={(event) => { setDocument({ ...document, title: event.target.value }); setSaved(false) }} /><PlatformBadge platform={document.platform} /></div>
        <div className="editor-actions">
          <button className="secondary-button" type="button" onClick={() => void copyPlainText()}><Copy size={16} /> 复制全文</button>
          <button className="secondary-button" type="button" onClick={() => void summarizeWithDeepSeek()} title="把文案与总结指令带到 DeepSeek（内容会发送给第三方服务）"><Bot size={16} /> AI 总结</button>
        </div>
      </header>
      {(actionError || actionMessage) && <div className={`editor-feedback ${actionError ? 'error' : 'success'}`} role={actionError ? 'alert' : 'status'}>{actionError || actionMessage}</div>}
      <div className="editor-workspace">
        <aside className="editor-source-panel">
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
            <span>来源</span>
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
              <audio ref={audioRef} className="media-player" controls preload="metadata" onTimeUpdate={onAudioTimeUpdate} src={api.mediaUrl(document.id)} />
            </div>
          )}
        </aside>
        <main className="transcript-editor" aria-label="文案编辑器">
          <div className="transcript-heading">
            <div><h1>校对文案</h1><span className="editor-subtitle">{view === 'timeline' ? `时间轴 · 共 ${document.segments.length} 段 · ${wordCount} 字` : `全文 · 整篇编辑（${document.segments.length} 段）· ${wordCount} 字`}</span></div>
            <div className="heading-side">
              <button
                className="text-button"
                type="button"
                onClick={() => void polishText()}
                disabled={formatting || document.segments.length === 0}
                title="把文案整理成带标点的自然段（正文没有标点时需先在设置里安装「中文标点恢复」模型）"
              >
                <Sparkles size={15} /> {formatting ? '整理中' : '智能分句'}
              </button>
              <div className="view-switch" role="tablist" aria-label="展示方式">
                <button type="button" role="tab" aria-selected={view === 'plain'} className={view === 'plain' ? 'active' : ''} onClick={() => void switchView('plain')}>全文</button>
                <button type="button" role="tab" aria-selected={view === 'timeline'} className={view === 'timeline' ? 'active' : ''} onClick={() => void switchView('timeline')}>时间轴</button>
              </div>
              <span className={`autosave-status${saved ? '' : ' unsaved'}`}><Check size={14} /> {saving ? '保存中…' : saved ? '已自动保存' : '正在保存…'}</span>
            </div>
          </div>
          {view === 'timeline' ? (
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
        <aside className="editor-inspector">
          <h2>文案信息</h2>
          <div className="inspector-block"><span>原视频时长</span><strong>{duration(document.duration_seconds)}</strong><small>用于估算校对所需时间</small></div>
          <div className="inspector-block"><span>识别原文</span><strong>已保留</strong><small>导出 JSON 时会附带识别原文，方便对比改动前后</small></div>
          <div className="inspector-block"><span>导出格式</span><div className="format-chips">{(['txt', 'srt', 'vtt', 'json'] as const).map((format) => <button key={format} type="button" onClick={() => void download(format)} disabled={Boolean(exporting)}>{exporting === format ? '…' : format.toUpperCase()}</button>)}</div></div>
        </aside>
      </div>
    </div>
  )
}
