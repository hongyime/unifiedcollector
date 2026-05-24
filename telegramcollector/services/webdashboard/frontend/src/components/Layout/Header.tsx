import { RefreshCw } from 'lucide-react'

interface HeaderProps {
  title: string
  subtitle?: string
  lastUpdated?: Date | null
  onRefresh?: () => void
  isRefreshing?: boolean
  actions?: React.ReactNode
}

export default function Header({ title, subtitle, lastUpdated, onRefresh, isRefreshing, actions }: HeaderProps) {
  const timeStr = lastUpdated
    ? lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : null
  return (
    <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-bg-surface sticky top-0 z-10">
      <div>
        <h1 className="text-lg font-semibold text-white">{title}</h1>
        {subtitle && <p className="text-text-secondary text-sm mt-0.5">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-3">
        {timeStr && <span className="text-text-muted text-xs font-mono">updated {timeStr}</span>}
        {actions}
        {onRefresh && (
          <button
            onClick={onRefresh}
            disabled={isRefreshing}
            className="flex items-center gap-2 px-3 py-1.5 text-sm text-text-secondary hover:text-white border border-border hover:border-border-strong rounded-md transition-colors disabled:opacity-50"
          >
            <RefreshCw size={13} className={isRefreshing ? 'animate-spin' : ''} />
            <span>Refresh</span>
          </button>
        )}
      </div>
    </div>
  )
}
