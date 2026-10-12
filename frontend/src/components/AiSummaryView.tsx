import { Bot, Check, Copy, LoaderCircle, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import { Modal, ModalHeader } from './Modal'
import { api } from '../lib/api'
import { cachedSummary, rememberSummary, type AiSummaryTarget } from '../lib/ai'
import { copyText } from '../lib/deepseek'
import type { AiSummaryResult } from '../types'

type SummaryState =
  | { status: 'loading' }
  | { status: 'done'; result: AiSummaryResult }
  | { status: 'error'; message: string }

/** 初始状态直接读缓存：命中就显示结果，没有才等请求回来。 */
function stateFor(key: string): SummaryState {
  const cached = cachedSummary(key)
  return cached ? { status: 'done', result: cached } : { status: 'loading' }
}

/**
 * 本机驱动的总结结果：挂载时先看缓存，命中就直接显示，不再打一次引擎。
 *
 * 缓存是必须的：面板关掉再打开（预览弹窗里收起、编辑页里关弹窗）如果重新请求，
 * 用户会为了「再看一眼」白白再付一次额度。只有正文变了（缓存键变化）或用户点
 * 「重新生成」时才真正调用引擎。
 *
 * 编辑页放在弹窗里、预览弹窗里内联展开，所以这里只画内容，外壳（标题栏、底栏）
 * 由调用方决定；复制也放在这里，两处行为才一致。
 */
export function AiSummaryView({ target }: { target: AiSummaryTarget }) {
  const [state, setState] = useState<SummaryState>(() => stateFor(target.key))
  const [copied, setCopied] = useState(false)
  // 换了目标（缓存键变化）时在渲染期同步 state，别留一帧上一个文案的摘要
  const [activeKey, setActiveKey] = useState(target.key)
  if (activeKey !== target.key) {
    setActiveKey(target.key)
    setState(stateFor(target.key))
  }

  /**
   * 请求一次摘要并写入缓存。
   *
   * 这里不动 loading 状态：本函数会被 effect 直接调用，而在 effect 里同步 setState
   * 会引发级联渲染（eslint 也拦）。谁要显示「生成中」，谁在事件里先置状态。
   */
  const run = useCallback(
    () =>
      api
        .aiSummarize({ title: target.title, text: target.text })
        .then((result) => {
          rememberSummary(target.key, result)
          setState({ status: 'done', result })
        })
        .catch((reason: unknown) => {
          setState({
            status: 'error',
            message: reason instanceof Error ? reason.message : 'AI 总结失败，请稍后重试',
          })
        }),
    [target],
  )

  /** 用户显式要（重新）生成：先回到加载态，再请求一次。 */
  function regenerate() {
    setCopied(false)
    setState({ status: 'loading' })
    void run()
  }

  useEffect(() => {
    // 缓存命中（换个面板再看同一条摘要）时不发请求；要不要重生成由用户决定
    if (cachedSummary(target.key)) return
    void run()
  }, [target.key, run])

  async function copy() {
    if (state.status !== 'done') return
    if (!(await copyText(state.result.text))) return
    setCopied(true)
    window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="ai-summary">
      <p className="ai-summary-source">
        <Bot size={15} />
        <span>{state.status === 'done' ? state.result.provider_name : target.provider.name}</span>
        {state.status === 'done' && state.result.model && <small>{state.result.model}</small>}
      </p>

      {state.status === 'loading' && (
        <div className="ai-summary-state" role="status">
          <LoaderCircle className="spin" size={18} />
          <span>正在生成摘要…</span>
          <small>由本机引擎在后台完成，不会跳转到浏览器。</small>
        </div>
      )}

      {state.status === 'error' && (
        <>
          <p className="form-message error" role="alert">{state.message}</p>
          <div className="ai-summary-actions">
            <button className="secondary-button" type="button" onClick={regenerate}>
              <RefreshCw size={15} /> 重试
            </button>
            <small>可到「设置 - AI 驱动」更换驱动，或检查本机登录态后重试。</small>
          </div>
        </>
      )}

      {state.status === 'done' && (
        <>
          <div className="ai-summary-text">{state.result.text}</div>
          <div className="ai-summary-actions">
            <button className="secondary-button" type="button" onClick={() => void copy()}>
              {copied ? <Check size={15} /> : <Copy size={15} />}
              {copied ? '已复制' : '复制摘要'}
            </button>
            {/* 重新生成是显式动作：会再调用一次引擎、再消耗一次额度，所以默认不自动做 */}
            <button
              className="text-button"
              type="button"
              onClick={regenerate}
              title="重新调用一次 AI（会再消耗一次额度）"
            >
              <RefreshCw size={14} /> 重新生成
            </button>
          </div>
        </>
      )}
    </div>
  )
}

/**
 * 编辑页用的弹窗：本机驱动没有「跳转」这一步，结果必须有地方落，独立开一个弹窗最省事。
 * 预览弹窗里不要用这个（会叠成两个弹窗），直接用上面的 AiSummaryView 内联展开。
 */
export function AiSummaryModal({ target, onClose }: { target: AiSummaryTarget; onClose: () => void }) {
  return (
    <Modal ariaLabel="AI 总结" onClose={onClose}>
      <ModalHeader title="AI 总结" icon={Bot} onClose={onClose} />
      <div className="modal-body">
        <AiSummaryView target={target} />
      </div>
      <footer className="modal-foot">
        <button className="primary-button" type="button" onClick={onClose}>完成</button>
      </footer>
    </Modal>
  )
}
