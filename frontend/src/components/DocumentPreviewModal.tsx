import { Bot, Copy, Download, ExternalLink, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { PlatformBadge } from './PlatformBadge'
import { api } from '../lib/api'
import { describeSummaryOutcome, openDeepSeekSummary, type SummaryOutcome } from '../lib/deepseek'
import { segmentsToPlainText } from '../lib/textFormatting'
import { useModalDismiss } from '../lib/useModalDismiss'
import type { DocumentDetail } from '../types'

const updatedAtFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

/**
 * 文案详情弹窗：只读预览正文，复制/导出都在这里完成；
 * 要改内容时再进完整编辑页，避免为看一眼内容而整页跳转。
 */
export function DocumentPreviewModal({
  documentId,
  backTo,
  backLabel,
  onClose,
}: {
  documentId: string
  backTo: string
  backLabel: string
  onClose: () => void
}) {
  const [detail, setDetail] = useState<DocumentDetail | null>(null)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  // AI 总结的临时反馈：贴在按钮上（与「已复制」同一套做法，弹窗里不再加提示条）
  const [summaryNote, setSummaryNote] = useState('')
  // 退场期间为 true：样式里据此播 150ms 的淡出 + 上收，然后才真正卸载
  const [closing, setClosing] = useState(false)
  const closingRef = useRef(false)

  /**
   * 关闭前先播一段轻退场再卸载。退出要比进入轻——用户此刻的注意力已经在下一件事上，
   * 弹窗不该用一整套进场动效的镜像把注意力抢回来。
   */
  const requestClose = useCallback(() => {
    if (closingRef.current) return
    closingRef.current = true
    setClosing(true)
    window.setTimeout(onClose, 150)
  }, [onClose])

  // 组件以 key={documentId} 挂载：切换预览对象时整体重建，无需在 effect 里重置状态
  useEffect(() => {
    let active = true
    api
      .document(documentId)
      .then((next) => {
        if (active) setDetail(next)
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message)
      })
    return () => {
      active = false
    }
  }, [documentId])

  // Esc 关闭 + 弹窗期间锁住背景滚动，两条都由 useModalDismiss 统一处理
  useModalDismiss(true, requestClose)

  async function copy() {
    if (!detail) return
    try {
      await navigator.clipboard.writeText(detail.segments.map((segment) => segment.text).join('\n'))
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setError('复制失败：浏览器没有授权剪贴板访问')
    }
  }

  /** AI 总结：把文案与总结指令带到 DeepSeek（预填或剪贴板，见 lib/deepseek）。 */
  async function summarize() {
    if (!detail) return
    setError('')
    const outcome = await openDeepSeekSummary(detail.title, segmentsToPlainText(detail.segments))
    const { message, isError } = describeSummaryOutcome(outcome)
    if (isError) {
      setSummaryNote('')
      setError(message)
      return
    }
    const notes: Partial<Record<SummaryOutcome['kind'], string>> = {
      prefilled: '已填入输入框',
      copied: '已复制，去粘贴',
      'copy-failed': '请手动复制',
    }
    setSummaryNote(notes[outcome.kind] ?? '')
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
  // 所以加载期间整个弹窗都不渲染，等数据（或错误）到了再一次性出现。
  if (!detail && !error) return null

  return (
    <div className={closing ? 'modal-overlay closing' : 'modal-overlay'} onClick={requestClose} role="presentation">
      <section
        className={closing ? 'modal-dialog closing' : 'modal-dialog'}
        role="dialog"
        aria-modal="true"
        aria-label="文案预览"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <div className="modal-title">
            <strong title={detail?.title}>{detail?.title ?? '无法预览'}</strong>
            {detail && <PlatformBadge platform={detail.platform} />}
          </div>
          <button className="icon-button" type="button" onClick={requestClose} aria-label="关闭预览">
            <X size={17} />
          </button>
        </header>

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
                detail.segments.map((segment) => <p key={segment.id}>{segment.text}</p>)
              )}
            </div>
            <footer className="modal-foot">
              <button className="text-button" type="button" onClick={() => void copy()}>
                <Copy size={15} /> {copied ? '已复制' : '复制全文'}
              </button>
              <button
                className="text-button"
                type="button"
                onClick={() => void summarize()}
                title="把文案与总结指令带到 DeepSeek（内容会发送给第三方服务）"
              >
                <Bot size={15} /> {summaryNote || 'AI 总结'}
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
              <Link
                className="primary-button"
                to={`/documents/${documentId}`}
                state={{ from: backTo, label: backLabel }}
                onClick={requestClose}
              >
                <ExternalLink size={15} /> 打开编辑
              </Link>
            </footer>
          </>
        )}
      </section>
    </div>
  )
}
