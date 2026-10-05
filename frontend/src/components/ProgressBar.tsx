import type { CSSProperties } from 'react'

/**
 * 统一的进度条。全应用只此一处渲染进度：
 * 之前 5 个页面各写一遍 markup，且只有 3 处带 role / aria-valuenow，
 * 读屏软件在别处拿不到进度值，这里一次性补齐。
 */
export function ProgressBar({
  value,
  label,
  /** 进度很小（刚起步、不到 1%）时也画出一小截，让用户看到「已经在动」。 */
  minPercent = 0,
  className = '',
}: {
  value: number
  label: string
  minPercent?: number
  className?: string
}) {
  const percent = Math.max(0, Math.min(100, Math.round(value)))
  const width = Math.max(percent, Math.min(minPercent, 100))
  return (
    <div
      className={className ? `progress-track ${className}` : 'progress-track'}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      aria-valuetext={`${percent}%`}
    >
      {/*
        只往 CSS 传一个 0~1 的比例，平移量交给样式算。
        进度是每帧都在改的值，走 transform 只触发合成，不会让轨道反复重排；
        轨道自身 overflow: hidden，填充左移之后右端的圆角仍是圆的。
      */}
      <span style={{ '--p': width / 100 } as CSSProperties} />
    </div>
  )
}
