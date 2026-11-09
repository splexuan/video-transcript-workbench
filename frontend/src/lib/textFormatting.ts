/**
 * 文案正文的统一拼装：一条分段就是一块正文，段落之间是否空行由
 * documents.paragraph_body 决定。
 *
 * 编辑页、复制全文、AI 总结、导出都用这一份规则；后端
 * text_formatting.py 与之同源，保证同一篇文案在任何出口都是同一份文字。
 */

export interface ParagraphInput {
  id: number
  start_ms: number | null
  end_ms: number | null
  text: string
}

/** 文案正文的「块」：一条分段就是一块，空分段不占位置。 */
export function transcriptBlocks(segments: ParagraphInput[]): string[] {
  return segments.map((segment) => segment.text.trim()).filter(Boolean)
}

/** 正文文本：paragraph_body 为真（「智能分句」整理过的段落形态）时段落之间空一行。 */
export function transcriptAsText(segments: ParagraphInput[], paragraphs = false): string {
  return transcriptBlocks(segments).join(paragraphs ? '\n\n' : '\n')
}
