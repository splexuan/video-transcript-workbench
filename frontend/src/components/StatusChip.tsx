import type { ReactNode } from 'react'

/**
 * 任务/批次状态芯片。原来在任务队列、批次右栏、单任务右栏各写一遍
 * `<span className={`status-chip ${status}`}>`，样式类与标签映射容易走偏。
 */
export function StatusChip({ status, children }: { status: string; children: ReactNode }) {
  return <span className={`status-chip ${status}`}>{children}</span>
}
