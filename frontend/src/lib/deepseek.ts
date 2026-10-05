/**
 * 跳到 DeepSeek 做总结。
 *
 * 网页端无法跨域写入别的网站输入框，但 DeepSeek 聊天页支持用 URL 参数预填问题
 * （其前端读取 q / prompt / mode / search / thinking / autosend / source），
 * 所以正常情况用 `?q=` 直接把指令填进输入框，用户确认后发送即可。
 * 文案很长时 URL 会被服务端拒绝，这时退回「复制到剪贴板 + 打开首页」，
 * 用户粘贴（Ctrl+V）后发送，效果一样只是多一步。
 */

export const DEEPSEEK_CHAT_URL = 'https://chat.deepseek.com/'

/**
 * URL 长度上限。中文经百分号编码后一字 9 字节，长文案很容易把地址撑到几十 KB，
 * 而不少服务端与代理会直接拒绝过长的 URL，所以留足余量后再退回剪贴板方案。
 */
export const MAX_PREFILL_URL_LENGTH = 6000

export function buildSummaryPrompt(title: string, text: string): string {
  const name = title.trim() || '未命名视频'
  return ['请帮我总结这篇文案', '', `标题：${name}`, '', '文案：', text.trim()].join('\n')
}

/** 能预填时返回带 q 参数的地址；太长时返回 null，由调用方走剪贴板。 */
export function deepseekPrefillUrl(prompt: string): string | null {
  if (!prompt.trim()) return null
  const url = `${DEEPSEEK_CHAT_URL}?q=${encodeURIComponent(prompt)}`
  return url.length <= MAX_PREFILL_URL_LENGTH ? url : null
}

/** 复制文本：优先剪贴板 API，不可用或未授权时退回临时 textarea。 */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // 常见于非安全上下文或用户拒绝授权，继续尝试兜底方案
  }
  try {
    const holder = document.createElement('textarea')
    holder.value = text
    holder.setAttribute('readonly', '')
    holder.style.position = 'fixed'
    holder.style.top = '0'
    holder.style.opacity = '0'
    document.body.appendChild(holder)
    holder.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(holder)
    return ok
  } catch {
    return false
  }
}

export type SummaryOutcome =
  /** 已带 q 参数打开：指令直接填进了输入框 */
  | { kind: 'prefilled' }
  /** 文案太长走剪贴板：已复制，需要用户自己粘贴 */
  | { kind: 'copied' }
  /** 打开了首页但复制没成功 */
  | { kind: 'copy-failed' }
  /** 新标签页被浏览器拦截 */
  | { kind: 'blocked'; copied: boolean }

/**
 * 打开 DeepSeek 并尽量把总结指令一并带过去。
 *
 * 注意开窗必须发生在用户点击的同步流程里：`await` 之后再 `window.open` 会被弹窗拦截，
 * 所以先开窗（长文案就先开首页），再去做复制。
 */
export async function openDeepSeekSummary(title: string, text: string): Promise<SummaryOutcome> {
  const prompt = buildSummaryPrompt(title, text)
  const prefill = deepseekPrefillUrl(prompt)
  const tab = window.open(prefill ?? DEEPSEEK_CHAT_URL, '_blank', 'noopener,noreferrer')
  if (!tab) return { kind: 'blocked', copied: await copyText(prompt) }
  if (prefill) return { kind: 'prefilled' }
  return (await copyText(prompt)) ? { kind: 'copied' } : { kind: 'copy-failed' }
}

/** 把结果翻译成给用户看的一句话；界面各处共用，避免同一件事两种说法。 */
export function describeSummaryOutcome(outcome: SummaryOutcome): { message: string; isError: boolean } {
  switch (outcome.kind) {
    case 'prefilled':
      return { message: '已在 DeepSeek 填好总结指令，确认后发送即可', isError: false }
    case 'copied':
      return { message: '文案较长，已复制总结指令；在 DeepSeek 里粘贴（Ctrl+V）后发送即可', isError: false }
    case 'copy-failed':
      return { message: '已打开 DeepSeek；自动复制失败，请手动复制文案后粘贴', isError: false }
    default:
      return {
        message: outcome.copied
          ? '已复制总结指令，但浏览器拦住了新标签页；允许弹窗后重试即可'
          : '浏览器拦住了新标签页，请允许本站打开新窗口后重试',
        isError: true,
      }
  }
}
