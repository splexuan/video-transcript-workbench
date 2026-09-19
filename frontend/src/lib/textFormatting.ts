/**
 * 全文视图的段落分组，规则与后端导出（text_formatting.py）保持一致：
 * 停顿超过 1.6 秒、或当前段落满 6 句 / 260 字时另起一段。
 * 只影响展示，不改动分段数据。
 */

export const PARAGRAPH_GAP_MS = 1_600
export const PARAGRAPH_MAX_SENTENCES = 5
export const PARAGRAPH_MAX_CHARS = 180

/** 只有拼接处两侧都是英文/数字时才补空格，中文片段直接相接。 */
function needsSpace(left: string, right: string): boolean {
  const isAsciiWord = (value: string) => /^[A-Za-z0-9]$/.test(value)
  return isAsciiWord(left) && isAsciiWord(right)
}

export interface ParagraphInput {
  id: number
  start_ms: number | null
  end_ms: number | null
  text: string
}

export interface Paragraph {
  /** 组成这一段的原始分段 id，全文模式编辑后要按它写回。 */
  ids: number[]
  sentences: string[]
  start_ms: number | null
  end_ms: number | null
}

export function groupParagraphs(
  segments: ParagraphInput[],
  merge = true,
): Paragraph[] {
  // 字幕来源不合并：字幕没有标点、条目边界也和语义无关，硬拼成段落只会更难读，
  // 保持「一行一句」反而接近它原本的形态。
  if (!merge) {
    return segments
      .map((segment) => ({ segment, text: segment.text.trim() }))
      .filter((item) => item.text)
      .map((item) => ({
        ids: [item.segment.id],
        sentences: [item.text],
        start_ms: item.segment.start_ms,
        end_ms: item.segment.end_ms,
      }))
  }

  const paragraphs: Paragraph[] = []
  let current: Paragraph | null = null
  let previousEnd: number | null = null

  for (const segment of segments) {
    const sentence = segment.text.trim()
    if (!sentence) continue

    const breakHere =
      current !== null &&
      ((previousEnd !== null &&
        segment.start_ms !== null &&
        segment.start_ms - previousEnd >= PARAGRAPH_GAP_MS) ||
        current.sentences.length >= PARAGRAPH_MAX_SENTENCES ||
        current.sentences.join('').length >= PARAGRAPH_MAX_CHARS)

    if (breakHere || current === null) {
      if (current) paragraphs.push(current)
      current = { ids: [], sentences: [], start_ms: segment.start_ms, end_ms: segment.end_ms }
    }

    current.ids.push(segment.id)
    current.sentences.push(sentence)
    if (current.start_ms === null) current.start_ms = segment.start_ms
    current.end_ms = segment.end_ms ?? current.end_ms
    if (segment.end_ms !== null) previousEnd = segment.end_ms
  }

  if (current) paragraphs.push(current)
  return paragraphs
}

/** 按句末标点切回句子；与 joinParagraph 的拼接规则相逆。 */
export function splitSentences(text: string): string[] {
  return text
    .split(/(?<=[。！？；…!?;])\s*/)
    .map((item) => item.trim())
    .filter(Boolean)
}

/**
 * 把一个段落的句子连成文本。中文片段之间**直接相接**：字幕整段往往没有标点，
 * 如果按「无标点就补空格」处理，连续的语音会被切成「一行一行」的碎片。
 */
export function joinParagraph(sentences: string[]): string {
  let joined = ''
  for (const sentence of sentences) {
    if (joined && needsSpace(joined[joined.length - 1], sentence[0] ?? '')) joined += ' '
    joined += sentence
  }
  return joined
}

/**
 * 把分段拼成一份可读的纯文本：段落之间空行，段内句子直接相接。
 * 编辑页的「复制全文 / AI 总结」与预览弹窗的 AI 总结共用这一份拼装规则，
 * 保证同一个文案在任何入口拿到的都是同一份文字。
 */
export function segmentsToPlainText(segments: ParagraphInput[]): string {
  return groupParagraphs(segments)
    .map((paragraph) => joinParagraph(paragraph.sentences))
    .join('\n\n')
}
