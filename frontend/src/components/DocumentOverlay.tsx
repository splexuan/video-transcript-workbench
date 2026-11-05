import { Bot, Copy, Download, Pencil } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { Modal, ModalHeader } from './Modal'
import { PlatformBadge } from './PlatformBadge'
import { DocumentEditor } from '../pages/EditorPage'
import { api } from '../lib/api'
import { useAiSummary } from '../lib/ai'
import { describeSummaryOutcome, type SummaryOutcome } from '../lib/deepseek'
import { useConfirm } from '../lib/useConfirm'
import { transcriptAsText, transcriptBlocks } from '../lib/textFormatting'
import type { DocumentDetail } from '../types'

const updatedAtFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

export type OverlayMode = 'read' | 'edit'

/**
 * 文案弹窗：把原来分开的「只读预览」和「整页编辑」合成同一个弹窗。
 *
 * 之前点「打开编辑」会跳到 /documents/:id，列表页整个卸载；返回时滚动位置虽然能
 * 还原，但「刚才点的是哪一条」只存在组件状态里，早没了。现在列表页始终挂在弹窗
 * 后面，阅读 → 编辑 → 关闭全程不换路由，回来就是原来的列表和刚才那一行。
 *
 * 阅读态仍保留复制、导出、AI 总结这些轻动作；编辑态复用整页编辑器，
 * 两者之间用标题栏的「阅读」按钮往返，切换前会先把改动落盘。
 */
export function DocumentOverlay({
  documentId,
  initialMode = 'read',
  onClose,
}: {
  documentId: string
  initialMode?: OverlayMode
  /** edited 表示这次弹窗里进过编辑态：列表据此决定要不要刷新那一行 */
  onClose: (edited: boolean) => void
}) {
  const [mode, setMode] = useState<OverlayMode>(initialMode)
  const [detail, setDetail] = useState<DocumentDetail | null>(null)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  // AI 总结的临时反馈：贴在按钮上（与「已复制」同一套做法，弹窗里不再加提示条）
  const [summaryNote, setSummaryNote] = useState('')
  // 退场期间为 true：样式里据此播 150ms 的淡出 + 上收，然后才真正卸载
  const [closing, setClosing] = useState(false)
  // 从编辑态回到阅读态时 +1，触发一次重新取数（编辑结果要反映到预览里）
  const [readVersion, setReadVersion] = useState(0)
  const closingRef = useRef(false)
  // 编辑器报告的未落盘状态：关闭按钮 / Esc / 点遮罩共用这一个守卫
  const dirtyRef = useRef(false)
  // 是否进过编辑态：关掉弹窗后列表需要刷新标题、字数
  const editedRef = useRef(initialMode === 'edit')
  // 当前模式与初始模式的 ref：关闭守卫和取数 effect 里要读最新值，但不该因此重跑
  const modeRef = useRef<OverlayMode>(initialMode)
  const initialModeRef = useRef<OverlayMode>(initialMode)
  // AI 总结：把指令带到 DeepSeek 网页，结果在那边展示
  const aiSummary = useAiSummary()
  const confirm = useConfirm()

  // 直接以编辑态打开时由编辑器自己取数，这里不必重复请求
  useEffect(() => {
    if (initialModeRef.current === 'edit' && readVersion === 0) return
    let active = true
    api
      .document(documentId)
      .then((next) => {
        if (active) {
          setDetail(next)
          setError('')
        }
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message)
      })
    return () => {
      active = false
    }
  }, [documentId, readVersion])

  /**
   * 关闭前先播一段轻退场再卸载。退出要比进入轻——用户此刻的注意力已经在下一件事上，
   * 弹窗不该用一整套进场动效的镜像把注意力抢回来。
   */
  const beginClose = useCallback(() => {
    if (closingRef.current) return
    closingRef.current = true
    setClosing(true)
    window.setTimeout(() => onClose(editedRef.current), 150)
  }, [onClose])

  // 有未保存的改动时先问一句；平时 0.6 秒自动保存已经兜住，很少会走到这里
  const requestClose = useCallback(() => {
    if (closingRef.current) return
    if (modeRef.current === 'edit' && dirtyRef.current) {
      void confirm({
        title: '还有未保存的更改',
        description: '现在关闭会丢掉刚才的修改；识别原文与已经保存过的内容不受影响。',
        confirmLabel: '关闭弹窗',
        cancelLabel: '留下继续编辑',
        tone: 'danger',
      }).then((ok) => {
        if (ok) beginClose()
      })
      return
    }
    beginClose()
  }, [beginClose, confirm])

  function openEditor() {
    editedRef.current = true
    dirtyRef.current = false
    modeRef.current = 'edit'
    setError('')
    setMode('edit')
  }

  /**
   * 编辑 → 阅读：编辑器已经保存过才调到这里。
   * 先拿它交回来的详情立刻渲染，避免退回阅读态时整块空白闪一下；
   * 同时再取一次，带上服务端计算的字数等信息。
   */
  function openReader(latest: DocumentDetail | null) {
    if (latest) setDetail(latest)
    dirtyRef.current = false
    modeRef.current = 'read'
    setMode('read')
    setReadVersion((version) => version + 1)
  }

  /** 预览里的正文：与编辑页、导出共用同一套拼装（一行一句；整理过段落之间空行）。 */
  function plainText(): string {
    return detail ? transcriptAsText(detail.segments, detail.paragraph_body) : ''
  }

  async function copy() {
    if (!detail) return
    try {
      await navigator.clipboard.writeText(plainText())
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setError('复制失败：浏览器没有授权剪贴板访问')
    }
  }

  /**
   * AI 总结：把文案与指令带到 DeepSeek 网页（预填或剪贴板，见 lib/deepseek）。
   */
  async function summarize() {
    if (!detail) return
    setError('')
    setSummaryNote('')
    const result = await aiSummary.start(detail.title, plainText())
    const { message, isError } = describeSummaryOutcome(result.outcome)
    if (isError) {
      setError(message)
      return
    }
    const notes: Partial<Record<SummaryOutcome['kind'], string>> = {
      prefilled: '已填入输入框',
      copied: '已复制，去粘贴',
      'copy-failed': '请手动复制',
    }
    setSummaryNote(notes[result.outcome.kind] ?? '')
    window.setTimeout(() => setSummaryNote(''), 3000)
  }

  function exportAs(format: 'txt' | 'srt' | 'vtt' | 'json') {
    // 用临时链接触发下载；直接改 window.location 会被 react-hooks 规则拦下
    const anchor = window.document.createElement('a')
    anchor.href = api.exportUrl(documentId, format)
    anchor.download = ''
    anchor.click()
  }

  // 本地取数很快，先把「正在打开文案…」画上去、再被真实标题替换，看起来就是闪一下。
  // 所以阅读态在数据（或错误）到之前不渲染；编辑态则交给编辑器自己的骨架。
  if (mode === 'read' && !detail && !error) return null

  return (
    <Modal
      ariaLabel={mode === 'edit' ? '编辑文案' : '文案预览'}
      closing={closing}
      onClose={requestClose}
      className={`document-overlay${mode === 'edit' ? ' is-editing' : ''}`}
    >
      {mode === 'edit' ? (
        <DocumentEditor
          documentId={documentId}
          initialDocument={detail}
          onClose={requestClose}
          onDirtyChange={(dirty) => {
            dirtyRef.current = dirty
          }}
          onSwitchToRead={openReader}
        />
      ) : (
        <>
          <ModalHeader
            title={<span title={detail?.title}>{detail?.title ?? '无法预览'}</span>}
            extra={detail ? <PlatformBadge platform={detail.platform} /> : undefined}
            onClose={requestClose}
            closeLabel="关闭预览"
          />

          {error && <p className="form-message error modal-error" role="alert">{error}</p>}

          {detail && (
            <>
              <div className="modal-meta">
                {detail.uploader && <span title={detail.uploader}>{detail.uploader}</span>}
                <span>{detail.word_count} 字</span>
                <span>{updatedAtFormatter.format(new Date(detail.updated_at))}</span>
                {detail.source_type === 'url' && (
                  <a href={detail.source_value} target="_blank" rel="noreferrer" title={detail.source_value}>
                    打开来源
                  </a>
                )}
              </div>
              <div className="modal-body">
                {detail.segments.length === 0 ? (
                  <p className="compact-empty">这条文案还没有内容。</p>
                ) : (
                  transcriptBlocks(detail.segments).map(
                    (block, index) => <p key={index}>{block}</p>,
                  )
                )}
              </div>
              <footer className="modal-foot">
                <button className="text-button" type="button" onClick={() => void copy()}>
                  <Copy size={15} /> {copied ? '已复制' : '复制全文'}
                </button>
                {/* 总结在 DeepSeek 网页里完成，提示只贴在按钮上（与「已复制」同一套做法） */}
                <button
                  className="text-button"
                  type="button"
                  onClick={() => void summarize()}
                  disabled={aiSummary.resolving}
                  title="把文案与总结指令带到 DeepSeek 网页，用你自己的账号完成总结"
                >
                  <Bot size={15} /> {aiSummary.resolving ? '准备中…' : summaryNote || 'AI 总结'}
                </button>
                {/* 导出入口给全四种格式，不必为了 SRT 再进编辑页 */}
                <span className="modal-export">
                  <Download size={15} />
                  <span className="sr-only">导出格式</span>
                  <span className="format-chips">
                    {(['txt', 'srt', 'vtt', 'json'] as const).map((format) => (
                      <button key={format} type="button" onClick={() => exportAs(format)} title={`导出 ${format.toUpperCase()}`}>
                        {format.toUpperCase()}
                      </button>
                    ))}
                  </span>
                </span>
                {/* 原地切成编辑态：不跳页，关掉弹窗就回到刚才的列表 */}
                <button className="primary-button" type="button" onClick={openEditor}>
                  <Pencil size={15} /> 编辑正文
                </button>
              </footer>
            </>
          )}
        </>
      )}
    </Modal>
  )
}
