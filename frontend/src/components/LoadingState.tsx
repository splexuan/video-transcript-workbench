import { LoaderCircle } from 'lucide-react'

type LoadingStateProps = {
  label?: string
  rows?: number
  compact?: boolean
}

export function LoadingState({ label = '正在加载…', rows = 3, compact = false }: LoadingStateProps) {
  return (
    <div className={`loading-state${compact ? ' compact' : ''}`} role="status" aria-live="polite">
      <span className="loading-state-label"><LoaderCircle className="spin" size={16} />{label}</span>
      <div className="skeleton-stack" aria-hidden="true">
        {Array.from({ length: rows }, (_, index) => <span className="skeleton-row" key={index} />)}
      </div>
    </div>
  )
}
