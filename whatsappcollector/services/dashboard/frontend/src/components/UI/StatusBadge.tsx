type Status = 'up' | 'down' | 'unknown' | 'pending' | 'running' | 'completed' | 'failed' | 'active' | 'inactive' | string

interface StatusBadgeProps {
  status: Status
  pulse?: boolean
}

const STATUS_CONFIG: Record<string, { dot: string; text: string; label: string }> = {
  up:        { dot: 'bg-status-up',      text: 'text-status-up',      label: 'UP' },
  down:      { dot: 'bg-status-down',    text: 'text-status-down',    label: 'DOWN' },
  unknown:   { dot: 'bg-status-unknown', text: 'text-status-unknown', label: 'UNKNOWN' },
  pending:   { dot: 'bg-status-pending', text: 'text-status-pending', label: 'PENDING' },
  running:   { dot: 'bg-status-running', text: 'text-status-running', label: 'RUNNING' },
  completed: { dot: 'bg-status-up',      text: 'text-status-up',      label: 'DONE' },
  failed:    { dot: 'bg-status-down',    text: 'text-status-down',    label: 'FAILED' },
  active:    { dot: 'bg-status-up',      text: 'text-status-up',      label: 'ACTIVE' },
  inactive:  { dot: 'bg-status-unknown', text: 'text-status-unknown', label: 'INACTIVE' },
  downloaded:{ dot: 'bg-status-up',      text: 'text-status-up',      label: 'DONE' },
  queued:    { dot: 'bg-status-running', text: 'text-status-running', label: 'QUEUED' },
  processed: { dot: 'bg-status-up',      text: 'text-status-up',      label: 'DONE' },
}

export default function StatusBadge({ status, pulse }: StatusBadgeProps) {
  const cfg = STATUS_CONFIG[status?.toLowerCase()] ?? STATUS_CONFIG.unknown
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={[
          'w-1.5 h-1.5 rounded-full flex-shrink-0',
          cfg.dot,
          pulse ? 'animate-pulse' : '',
        ]
          .filter(Boolean)
          .join(' ')}
      />
      <span className={`text-xs font-mono font-medium ${cfg.text}`}>
        {cfg.label}
      </span>
    </span>
  )
}
