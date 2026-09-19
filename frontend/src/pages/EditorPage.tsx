import { ArrowLeft, Bot, Check, CheckCheck, Copy, Film, RefreshCw, RotateCcw, Save, Sparkles, Undo2 } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'

import { PlatformBadge } from '../components/PlatformBadge'
import { api } from '../lib/api'
import { describeSummaryOutcome, openDeepSeekSummary } from '../lib/deepseek'
import { groupParagraphs, joinParagraph, segmentsToPlainText, splitSentences, type Paragraph } from '../lib/textFormatting'
import type { DocumentDetail, EditorNavState, TranscriptSegment } from '../types'

/** 让全文模式的段落框跟着内容长高，避免出现内部滚动条。 */
function autoGrow(element: HTMLTextAreaElement) {
  element.style.height = 'auto'
  element.style.height = `${element.scrollHeight}px`
}

/**
 * 把全文模式里改过的段落写回分段。
 *
 * 按句子顺序对齐：句子数不变（改错别字、调标点）时一一对应，时间轴完全不受影响；
 * 删了句子时只清空末尾多出来的分段；加了句子时把多出的部分并进最后一段，不丢内容。
 */
function applyParagraphDrafts(
  segments: TranscriptSegment[],
  paragraphs: Paragraph[],
  drafts: Record<string, string>,
): TranscriptSegment[] {
  if (Object.keys(drafts).length === 0) return segments

  const next = segments.map((segment) => ({ ...segment }))
  const byId = new Map(next.map((segment) => [segment.id, segment]))

  for (const [key, text] of Object.entries(drafts)) {
    const paragraph = paragraphs[Number(key)]
    if (!paragraph || text === undefined) continue

    const targets = paragraph.ids
      .map((id) => byId.get(id))
      .filter((item): item is TranscriptSegment => Boolean(item))
    if (targets.length === 0) continue

    const pieces = splitSentences(text)
    targets.forEach((segment, index) => {
      if (index >= pieces.length) {
        segment.text = ''
        return
      }
      const isLast = index === targets.length - 1
      segment.text = isLast && pieces.length > targets.length
        ? pieces.slice(index).join('')
        : pieces[index]
    })
  }

  return next
}

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
  const [statusBusy, setStatusBusy] = useState(false)
  // 播放中的分段：音轨播放时高亮当前句，点击时间码可跳到该句
  const [activeSegmentId, setActiveSegmentId] = useState<number | null>(null)
  const audioRef = useRef<HTMLAudioElement>(null)
  // 默认打开全文阅读，需要按时间定位或改字时再切到时间轴
  const [view, setView] = useState<TextView>('plain')
  // 全文模式的段落草稿：编辑时先留在本地，保存时才写回分段，
  // 否则打字过程中标点一变，分段和时间轴就会被反复重排。
  const [paragraphDrafts, setParagraphDrafts] = useState<Record<string, string>>({})
  const wordCount = useMemo(() => document?.segments.reduce((count, segment) => count + segment.text.trim().length, 0) ?? 0, [document])
  const description = useMemo(() => decodeEntities(document?.description ?? ''), [document?.description])
  // 两种视图都要拿到段落：时间轴下保存时同样要把全文模式的改动写回。
  // 字幕来源按原样一行一句（字幕没有标点，合并成段落会更难读）。
  const lineByLine = document?.transcript_source === 'subtitle'
  const paragraphs = useMemo(
    () => (document ? groupParagraphs(document.segments, !lineByLine) : []),
    [document, lineByLine],
  )

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

  async function persistDocument() {
    if (!document) return null
    const title = document.title.trim()
    if (!title) throw new Error('文案标题不能为空')
    // 全文模式的段落改动先落到分段上，再统一提交
    const segments = applyParagraphDrafts(document.segments, paragraphs, paragraphDrafts)
    const updated = await api.saveSegments(document.id, segments)
    const summary = await api.saveDocument(document.id, { title })
    const merged = { ...updated, ...summary, title }
    setDocument(merged)
    setParagraphDrafts({})
    setSaved(true)
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

  /** 当前编辑内容的纯文本（全文模式下未保存的段落改动也按已提交的分段为准）。 */
  function currentPlainText(): string {
    return document ? segmentsToPlainText(document.segments) : ''
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

  // Ctrl / Cmd + S 保存：编辑器里最容易形成肌肉记忆的快捷键。
  // 用 ref 保存最新的 save，避免每次渲染都重新绑定键盘监听。
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

  // 改动后 3 秒自动保存：长文校对容易忘记手动保存，静默落盘不打断编辑
  useEffect(() => {
    if (saved || saving) return
    const timer = window.setTimeout(() => void saveRef.current({ silent: true }), 3000)
    return () => window.clearTimeout(timer)
  }, [saved, saving])

  /** 智能分句：把手写/识别文本重排为句级分段，时间轴按句子对齐。 */
  async function autoFormat() {
    if (!document) return
    setFormatting(true)
    setActionError('')
    setActionMessage('')
    try {
      // 先落盘现有改动，避免整理结果被旧草稿覆盖
      await persistDocument()
      const updated = await api.autoFormat(document.id)
      setDocument(updated)
      setParagraphDrafts({})
      setSaved(true)
      setActionMessage('已按句子重新分段，可以直接逐句校对')
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '智能分句失败，请稍后重试')
    } finally {
      setFormatting(false)
    }
  }

  /** 标记已校对 / 退回草稿：给文案库里区分「还没看」和「已经过了一遍」。 */
  async function toggleReviewed() {
    if (!document) return
    const next = document.status === 'reviewed' ? 'draft' : 'reviewed'
    setStatusBusy(true)
    setActionError('')
    setActionMessage('')
    try {
      await persistDocument()
      const updated = await api.saveDocument(document.id, { status: next })
      setDocument((current) => (current ? { ...current, status: updated.status } : current))
      setActionMessage(next === 'reviewed' ? '已标记为校对完成' : '已退回草稿状态')
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '状态更新失败，请稍后重试')
    } finally {
      setStatusBusy(false)
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

  /** 切换视图前先把全文模式的改动落到分段上，免得「切到时间轴就看不见改动」。 */
  function switchView(next: TextView) {
    if (Object.keys(paragraphDrafts).length > 0) {
      setDocument((current) =>
        current
          ? { ...current, segments: applyParagraphDrafts(current.segments, paragraphs, paragraphDrafts) }
          : current,
      )
      setParagraphDrafts({})
    }
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
        <div className="editor-title-group"><input aria-label="文案标题" title={document.title} value={document.title} onChange={(event) => { setDocument({ ...document, title: event.target.value }); setSaved(false) }} /><PlatformBadge platform={document.platform} />{!saved && <span className="save-state">有未保存的更改</span>}</div>
        <div className="editor-actions">
          <button className="secondary-button" type="button" onClick={() => void toggleReviewed()} disabled={statusBusy}>
            {document.status === 'reviewed' ? <><Undo2 size={16} /> 退回草稿</> : <><CheckCheck size={16} /> 标记已校对</>}
          </button>
          <button className="secondary-button" type="button" onClick={() => void copyPlainText()}><Copy size={16} /> 复制全文</button>
          <button className="secondary-button" type="button" onClick={() => void summarizeWithDeepSeek()} title="把文案与总结指令带到 DeepSeek（内容会发送给第三方服务）"><Bot size={16} /> AI 总结</button>
          <button className="primary-button" type="button" onClick={() => void save()} disabled={saving}><Save size={16} /> {saving ? '保存中' : '保存'}</button>
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
            <div><h1>校对文案</h1><span className="editor-subtitle">{view === 'timeline' ? `时间轴 · 共 ${document.segments.length} 段 · ${wordCount} 字` : `全文 · ${paragraphs.length} ${lineByLine ? '行' : '个自然段'} · ${wordCount} 字`}</span></div>
            <div className="heading-side">
              <button className="text-button" type="button" onClick={() => void autoFormat()} disabled={formatting || document.segments.length === 0} title="把识别结果按句子重新分段，便于逐句校对">
                <Sparkles size={15} /> {formatting ? '整理中' : '智能分句'}
              </button>
              <div className="view-switch" role="tablist" aria-label="展示方式">
                <button type="button" role="tab" aria-selected={view === 'plain'} className={view === 'plain' ? 'active' : ''} onClick={() => switchView('plain')}>全文</button>
                <button type="button" role="tab" aria-selected={view === 'timeline'} className={view === 'timeline' ? 'active' : ''} onClick={() => switchView('timeline')}>时间轴</button>
              </div>
              <span className={`autosave-status${saved ? '' : ' unsaved'}`}><Check size={14} /> {saved ? '已保存到本机' : '等待保存'}</span>
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
                    <textarea value={segment.text} rows={1} onChange={(event) => { const segments = document.segments.map((item) => item.id === segment.id ? { ...item, text: event.target.value } : item); setDocument({ ...document, segments }); setSaved(false) }} aria-label={`第 ${index + 1} 段文案`} />
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
              {paragraphs.length === 0 ? (
                <p className="compact-empty">还没有内容</p>
              ) : (
                paragraphs.map((paragraph, index) => (
                  <textarea
                    className={`fulltext-paragraph${lineByLine ? ' fulltext-line' : ''}`}
                    key={paragraph.ids.join('-')}
                    value={paragraphDrafts[String(index)] ?? joinParagraph(paragraph.sentences)}
                    ref={(element) => {
                      if (element) autoGrow(element)
                    }}
                    onChange={(event) => {
                      const text = event.target.value
                      setParagraphDrafts((current) => ({ ...current, [String(index)]: text }))
                      setSaved(false)
                      autoGrow(event.target)
                    }}
                    aria-label={`第 ${index + 1} ${lineByLine ? '行' : '自然段'}`}
                  />
                ))
              )}
              <p className="fulltext-note">
                {lineByLine
                  ? '这条文案来自平台字幕，按字幕原样一行一句展示：字幕本身没有标点，合并成段落反而更难读。想要带标点的段落，可在「工作台」关掉「优先使用平台字幕」后重新提取。'
                  : '全文按自然段排版，可以直接修改；改动会写回对应分段。要按时间逐句定位请切到「时间轴」，保存用 Ctrl + S。'}
              </p>
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
