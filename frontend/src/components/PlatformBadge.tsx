import { platformLabels } from '../lib/labels'

/**
 * 平台徽标。颜色由 `.platform-*` 决定，一律走 CSS 变量，
 * 这里只负责给出可读的文本与悬停标题（徽标本身很窄，
 * 光看两个字有时分不清是哪个平台的全称）。
 */
export function PlatformBadge({ platform }: { platform: string }) {
  const label = platformLabels[platform] ?? platform
  return (
    <span className={`platform-badge platform-${platform}`} title={label}>
      {label}
    </span>
  )
}
