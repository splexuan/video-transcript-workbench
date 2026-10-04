/**
 * 全文视图的段落分组。识别来源的规则与后端导出（text_formatting.py）保持一致：
 * 停顿超过 1.6 秒、或当前段落满 5 句 / 180 字时另起一段；字幕来源另有
 * groupSubtitleLines，不看停顿、只按行数与字数封顶。分组只影响展示，不改动分段数据。
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

/** 识别来源的分组：按停顿与句数/字数上限成段（字幕来源请用 groupSubtitleLines）。 */
export function groupParagraphs(segments: ParagraphInput[]): Paragraph[] {
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

/**
 * 把整段文本按各分段的原始字数比例切成等量片段，用于整篇编辑的写回。
 *
 * 字幕没有句末标点，切不出句子；行数变了（合并、拆行、增删）时只能按
 * 「谁原来占多少字」把改写后的文本摊回原分段。
 * 两条硬约束：
 * - 片段拼接后与归一化文本逐字相等，不丢字、不重复；
 * - 片段数量恒等于分段数量，时间轴行数与 SRT 行数都不变。
 *
 * 换行与制表符在归一化时删除：用户在段落里换行只是为了排版，不该进入字幕文本。
 */
export function splitTextByWeight(originals: string[], text: string): string[] {
  const cleaned = text.replace(/[\r\n\t]+/g, '')
  if (originals.length === 0) return []
  if (originals.length === 1) return [cleaned]

  const weights = originals.map((item) => item.trim().length)
  const totalWeight = weights.reduce((sum, weight) => sum + weight, 0)
  const total = cleaned.length
  const pieces: string[] = []
  let cursor = 0

  // 原分段全是空的（少见）：没有比例可依，退回均分，同样保证拼接完整
  if (totalWeight === 0) {
    originals.forEach((_, index) => {
      const end = index === originals.length - 1
        ? total
        : Math.floor((total * (index + 1)) / originals.length)
      const stop = Math.max(cursor, Math.min(end, total))
      pieces.push(cleaned.slice(cursor, stop))
      cursor = stop
    })
    return pieces
  }

  let accumulated = 0
  originals.forEach((_, index) => {
    accumulated += weights[index] ?? 0
    // 最后一段吃掉余量，四舍五入的零头不会丢在中间
    const end = index === originals.length - 1
      ? total
      : Math.round((total * accumulated) / totalWeight)
    const stop = Math.max(cursor, Math.min(end, total))
    pieces.push(cleaned.slice(cursor, stop))
    cursor = stop
  })
  return pieces
}

/**
 * 整篇编辑（字幕文案整篇一个框）的写回：按空行分段，把每段摊回它覆盖的分段。
 *
 * - 段落数与打开时相同（改字、加句、删句）：每个段落按它原本覆盖的分段、按字数占比
 *   摊回，段落与时间的对应关系保持不变；
 * - 段落数变了（合并或拆开段落）：退化成「全篇按各分段原始字数占比摊回」，仍然不丢字，
 *   只是段落边界在重新打开时按分组规则重算。
 *
 * 返回 segment id → 新文本；没进结果的 id（原本就空的分段）保持不动。
 * 空行与换行只作排版用，不会进入分段文本。
 */
export function distributeWholeText(
  segments: ParagraphInput[],
  paragraphs: Paragraph[],
  text: string,
): Map<number, string> {
  const blocks = text
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
  const byId = new Map(segments.map((segment) => [segment.id, segment]))
  const next = new Map<number, string>()

  if (blocks.length > 0 && blocks.length === paragraphs.length) {
    paragraphs.forEach((paragraph, index) => {
      const targets = paragraph.ids
        .map((id) => byId.get(id))
        .filter((segment): segment is ParagraphInput => Boolean(segment))
      if (targets.length === 0) return
      const pieces = splitTextByWeight(targets.map((segment) => segment.text), blocks[index])
      targets.forEach((segment, order) => next.set(segment.id, pieces[order] ?? ''))
    })
    return next
  }

  const pieces = splitTextByWeight(segments.map((segment) => segment.text), text)
  segments.forEach((segment, index) => next.set(segment.id, pieces[index] ?? ''))
  return next
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
 * 文案正文的「块」：一条分段 / 一个自然段。与后端 transcript_blocks 同源。
 *
 * 字幕「一行一句」的边界就是分段边界：智能分句整理过的字幕，分段本身就是自然段，
 * 所以这里不用再区分——正文形态由分段决定，随时与编辑页一致。
 */
export function transcriptBlocks(segments: ParagraphInput[], subtitle: boolean): string[] {
  if (subtitle) {
    return segments.map((segment) => segment.text.trim()).filter(Boolean)
  }
  return groupParagraphs(segments)
    .map((paragraph) => joinParagraph(paragraph.sentences).trim())
    .filter(Boolean)
}

/**
 * 把分段拼成一份可读的纯文本：整理过的字幕与识别结果段落之间空一行，未整理的字幕一行一句。
 *
 * paragraphs 即 document.paragraph_body：字幕整理成自然段后段落之间要有空行，
 * 否则每段一行挤在一起，看着还是「一行一句」。
 *
 * 这是「文案正文」在前端唯一的拼装入口——编辑页的整篇编辑框、复制全文、AI 总结、
 * 预览弹窗都用它，后端导出（txt / json）用同一套规则，保证同一篇文案在任何出口
 * 拿到的都是同一份文字。
 */
export function transcriptAsText(
  segments: ParagraphInput[],
  subtitle: boolean,
  paragraphs = false,
): string {
  return transcriptBlocks(segments, subtitle).join(paragraphs || !subtitle ? '\n\n' : '\n')
}
