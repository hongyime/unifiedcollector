interface MetricCardProps {
  label: string
  value: string | number
  sublabel?: string
  trend?: 'up' | 'down' | 'neutral'
  mono?: boolean
  className?: string
}

export default function MetricCard({
  label,
  value,
  sublabel,
  trend,
  mono = false,
  className = '',
}: MetricCardProps) {
  const trendColor =
    trend === 'up'
      ? 'text-status-up'
      : trend === 'down'
      ? 'text-status-down'
      : 'text-text-secondary'

  return (
    <div className={`card p-4 flex flex-col gap-1 ${className}`}>
      <span className="text-text-muted text-xs uppercase tracking-wider font-medium">
        {label}
      </span>
      <span
        className={[
          'text-2xl font-semibold',
          mono ? 'font-mono' : '',
          trend ? trendColor : 'text-white',
        ]
          .filter(Boolean)
          .join(' ')}
      >
        {typeof value === 'number' ? value.toLocaleString() : value}
      </span>
      {sublabel && (
        <span className="text-text-muted text-xs">{sublabel}</span>
      )}
    </div>
  )
}
