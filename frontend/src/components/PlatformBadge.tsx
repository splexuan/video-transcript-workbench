import { platformLabels } from '../lib/labels'

export function PlatformBadge({ platform }: { platform: string }) {
  return <span className={`platform-badge platform-${platform}`}>{platformLabels[platform] ?? platform}</span>
}
