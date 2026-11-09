import { Fragment, type ReactNode } from 'react'

/**
 * 技能说明的轻量 Markdown 渲染。
 *
 * 项目没有 Markdown 依赖，而说明里只用得到标题、代码块、列表、引用和表格；
 * 为这些内容引一个解析库（再处理安全与样式）不划算。这里按行切块、用 React
 * 元素渲染，天然转义 HTML，链接也只放行 http / https / mailto。
 */

const INLINE_PATTERN =
  /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^)\n]+\)|https?:\/\/[^\s<>)]+)/g
const SAFE_SCHEME = /^(https?:|mailto:)/i

function inline(text: string, keyPrefix: string): ReactNode[] {
  return text.split(INLINE_PATTERN).map((part, index) => {
    const key = `${keyPrefix}-${index}`
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>
    }
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>
    }
    const markdownLink = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part)
    if (markdownLink) {
      const href = markdownLink[2].trim()
      return SAFE_SCHEME.test(href) ? (
        <a key={key} href={href} target="_blank" rel="noreferrer">{markdownLink[1]}</a>
      ) : (
        <Fragment key={key}>{markdownLink[1]}</Fragment>
      )
    }
    if (/^https?:\/\//.test(part)) {
      // 中文正文里的裸链接常把句末标点粘进去，显示时把标点留在链接外。
      const href = part.replace(/[，。；：、]+$/, '')
      const suffix = part.slice(href.length)
      return (
        <Fragment key={key}>
          <a href={href} target="_blank" rel="noreferrer">{href}</a>
          {suffix}
        </Fragment>
      )
    }
    return <Fragment key={key}>{part}</Fragment>
  })
}

function stripFrontmatter(text: string) {
  if (!text.startsWith('---')) return text
  const end = text.indexOf('\n---', 3)
  if (end === -1) return text
  return text.slice(end + 4).replace(/^\r?\n/, '')
}

function isBlockStart(line: string) {
  const trimmed = line.trim()
  return (
    /^(#{1,4})\s+/.test(trimmed) ||
    trimmed.startsWith('```') ||
    trimmed.startsWith('>') ||
    /^[-*]\s+/.test(trimmed) ||
    /^\d+\.\s+/.test(trimmed) ||
    trimmed.startsWith('|') ||
    trimmed === '---'
  )
}

function isSeparatorRow(cells: string[]) {
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell))
}

export function SkillMarkdown({ text }: { text: string }) {
  const lines = stripFrontmatter(text).split(/\r?\n/)
  const blocks: ReactNode[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    const trimmed = line.trim()

    if (!trimmed) {
      index += 1
      continue
    }

    if (trimmed === '---') {
      blocks.push(<hr key={`hr-${index}`} />)
      index += 1
      continue
    }

    const fence = /^```(\w*)/.exec(trimmed)
    if (fence) {
      const code: string[] = []
      index += 1
      while (index < lines.length && !lines[index].trim().startsWith('```')) {
        code.push(lines[index])
        index += 1
      }
      index += 1
      blocks.push(
        <pre className="skill-guide-code" key={`code-${index}`}>
          <code>{code.join('\n')}</code>
        </pre>,
      )
      continue
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(trimmed)
    if (heading) {
      const level = heading[1].length
      const content = inline(heading[2], `h-${index}`)
      blocks.push(
        level <= 2 ? (
          <h2 key={`h-${index}`}>{content}</h2>
        ) : level === 3 ? (
          <h3 key={`h-${index}`}>{content}</h3>
        ) : (
          <h4 key={`h-${index}`}>{content}</h4>
        ),
      )
      index += 1
      continue
    }

    if (trimmed.startsWith('>')) {
      const quote: string[] = []
      while (index < lines.length && lines[index].trim().startsWith('>')) {
        quote.push(lines[index].trim().replace(/^>\s?/, ''))
        index += 1
      }
      blocks.push(
        <blockquote key={`quote-${index}`}>
          <p>{inline(quote.join(' '), `q-${index}`)}</p>
        </blockquote>,
      )
      continue
    }

    if (trimmed.startsWith('|')) {
      const rows: string[][] = []
      while (index < lines.length && lines[index].trim().startsWith('|')) {
        rows.push(
          lines[index]
            .trim()
            .replace(/^\|/, '')
            .replace(/\|$/, '')
            .split('|')
            .map((cell) => cell.trim()),
        )
        index += 1
      }
      const header = rows[0] ?? []
      const body = rows.slice(1).filter((row) => !isSeparatorRow(row))
      blocks.push(
        <div className="skill-guide-table-wrap" key={`table-${index}`}>
          <table>
            <thead>
              <tr>
                {header.map((cell, cellIndex) => (
                  <th key={cellIndex}>{inline(cell, `th-${index}-${cellIndex}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{inline(cell, `td-${index}-${rowIndex}-${cellIndex}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    const unordered = /^[-*]\s+/.test(trimmed)
    const ordered = /^\d+\.\s+/.test(trimmed)
    if (unordered || ordered) {
      const items: string[] = []
      const pattern = unordered ? /^[-*]\s+/ : /^\d+\.\s+/
      while (index < lines.length && pattern.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(pattern, ''))
        index += 1
      }
      const children = items.map((item, itemIndex) => (
        <li key={itemIndex}>{inline(item, `li-${index}-${itemIndex}`)}</li>
      ))
      blocks.push(
        unordered
          ? <ul key={`ul-${index}`}>{children}</ul>
          : <ol key={`ol-${index}`}>{children}</ol>,
      )
      continue
    }

    const paragraph: string[] = [trimmed]
    index += 1
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraph.push(lines[index].trim())
      index += 1
    }
    blocks.push(<p key={`p-${index}`}>{inline(paragraph.join(' '), `p-${index}`)}</p>)
  }

  return <div className="skill-guide-body">{blocks}</div>
}
