/**
 * AI 驱动：读取「用哪个 AI 跑总结」，并按驱动类型分流。
 *
 * 分流规则很简单：
 * - browser 驱动（DeepSeek）：前端打开网页，把指令带过去（见 lib/deepseek.ts）；
 * - local 驱动（WorkBuddy / Codex）：后端拿本机登录态跑，结果直接回前端展示。
 *
 * 驱动清单只有登录态变化才变，所以进程内缓存一份；在设置页保存后调
 * invalidateAiProviders() 作废，避免拿着旧选择不放。
 */
import { useCallback, useState } from 'react'

import { api } from './api'
import { openDeepSeekSummary, type SummaryOutcome } from './deepseek'
import type { AiProvider, AiProviderCatalog, AiProviderType, AiSummaryResult } from '../types'

/** 清单读不到时的兜底：DeepSeek 永远可用，按钮不会因此变成死键。 */
const DEEPSEEK_FALLBACK: AiProvider = {
  type: 'deepseek',
  name: 'DeepSeek · 网页版',
  mode: 'browser',
  note: '',
  available: true,
  message: '',
  models: [],
  default_model: '',
}

let cached: Promise<AiProviderCatalog> | null = null

/** 读取驱动清单；force 用于设置页（每次进来都重新探测本机登录态）。 */
export function loadAiProviders(force = false): Promise<AiProviderCatalog> {
  if (!cached || force) {
    cached = api.aiProviders().catch((reason) => {
      // 失败不留缓存，下次点击可以重试
      cached = null
      throw reason
    })
  }
  return cached
}

export function invalidateAiProviders() {
  cached = null
}

/** 按类型取驱动；清单里没有就退回 DeepSeek，保证「AI 总结」始终有去处。 */
export function providerByType(catalog: AiProviderCatalog, type: AiProviderType): AiProvider {
  return (
    catalog.providers.find((provider) => provider.type === type) ??
    catalog.providers.find((provider) => provider.mode === 'browser') ??
    DEEPSEEK_FALLBACK
  )
}

/** 一次「AI 总结」的分流结果：跳了网页 / 打开本机结果 / 直接失败。 */
export type AiSummaryTrigger =
  | { kind: 'browser'; outcome: SummaryOutcome }
  | { kind: 'local' }
  | { kind: 'error'; message: string }

export interface AiSummaryTarget {
  /** 缓存键：同一条文案（正文没变）再次总结时直接复用结果，不再消耗额度。 */
  key: string
  title: string
  text: string
  provider: AiProvider
}

/* ---------- 摘要缓存 ---------- */

/** 缓存条数上限：结果都很小，留够一个工作时段来回看即可。 */
const SUMMARY_CACHE_LIMIT = 20
const summaryCache = new Map<string, AiSummaryResult>()

/**
 * 摘要的缓存键 = 文档 id + 正文长度 + 正文哈希。
 *
 * 正文改过之后键就变了，自然会重新总结；没改就复用，避免「关了再开又扣一次额度」。
 */
export function summaryKey(documentId: string, text: string): string {
  let hash = 5381
  for (let index = 0; index < text.length; index += 1) {
    hash = ((hash << 5) + hash + text.charCodeAt(index)) | 0
  }
  return `${documentId}:${text.length}:${(hash >>> 0).toString(36)}`
}

export function cachedSummary(key: string): AiSummaryResult | null {
  return summaryCache.get(key) ?? null
}

export function rememberSummary(key: string, result: AiSummaryResult) {
  if (summaryCache.size >= SUMMARY_CACHE_LIMIT) {
    const oldest = summaryCache.keys().next().value
    if (oldest !== undefined) summaryCache.delete(oldest)
  }
  summaryCache.set(key, result)
}

/**
 * 「AI 总结」的统一入口：解析设置里的驱动，再决定跳网页还是走本机。
 *
 * 组件只需要处理三件事：browser 用 describeSummaryOutcome 出提示，local 打开结果面板，
 * error 直接显示 message。这样编辑页与预览弹窗只有一份分流逻辑。
 *
 * documentId 参与缓存键，同一条文案反复开关面板不会重复调用引擎（也就不会重复消耗额度）。
 */
export function useAiSummary() {
  const [target, setTarget] = useState<AiSummaryTarget | null>(null)
  const [resolving, setResolving] = useState(false)

  const start = useCallback(
    async (documentId: string, title: string, text: string): Promise<AiSummaryTrigger> => {
      setResolving(true)
      try {
        const catalog = await loadAiProviders()
        const provider = providerByType(catalog, catalog.preference.provider)
        if (provider.mode === 'browser') {
          return { kind: 'browser', outcome: await openDeepSeekSummary(title, text) }
        }
        setTarget({ key: summaryKey(documentId, text), title, text, provider })
        return { kind: 'local' }
      } catch (reason) {
        return { kind: 'error', message: reason instanceof Error ? reason.message : '读取 AI 驱动设置失败' }
      } finally {
        setResolving(false)
      }
    },
    [],
  )

  const close = useCallback(() => setTarget(null), [])

  return { start, resolving, target, close }
}
