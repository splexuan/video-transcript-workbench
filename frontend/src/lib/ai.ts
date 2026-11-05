/**
 * 「AI 总结」：固定跳到 DeepSeek 网页完成。
 *
 * 网页端无法跨域写入别的网站输入框，DeepSeek 聊天页支持用 URL 参数预填问题，
 * 具体分流（预填 / 复制到剪贴板）见 lib/deepseek.ts。本机不做任何 AI 推理，
 * 也不保存第三方返回的结果。
 */
import { useCallback, useState } from 'react'

import { openDeepSeekSummary, type SummaryOutcome } from './deepseek'

export interface AiSummaryTrigger {
  kind: 'browser'
  outcome: SummaryOutcome
}

/**
 * 编辑页与预览弹窗共用的入口：调用方只需要把 outcome 翻译成一句话。
 *
 * 注意开窗必须发生在用户点击的同步流程里，所以这里不能先 await 别的异步操作；
 * openDeepSeekSummary 自己也是先开窗、再复制。
 */
export function useAiSummary() {
  const [resolving, setResolving] = useState(false)

  const start = useCallback(async (title: string, text: string): Promise<AiSummaryTrigger> => {
    setResolving(true)
    try {
      return { kind: 'browser', outcome: await openDeepSeekSummary(title, text) }
    } finally {
      setResolving(false)
    }
  }, [])

  return { start, resolving }
}
