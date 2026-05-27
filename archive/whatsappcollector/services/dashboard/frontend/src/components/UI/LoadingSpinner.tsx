interface LoadingSpinnerProps {
  size?: 'sm' | 'md' | 'lg'
  className?: string
  label?: string
}

const SIZES = {
  sm: 'w-4 h-4 border-[1.5px]',
  md: 'w-6 h-6 border-2',
  lg: 'w-10 h-10 border-[3px]',
}

export default function LoadingSpinner({
  size = 'md',
  className = '',
  label,
}: LoadingSpinnerProps) {
  return (
    <div className={`flex flex-col items-center justify-center gap-3 ${className}`}>
      <div
        className={[
          'rounded-full border-white/20 border-t-white/80 animate-spin',
          SIZES[size],
        ].join(' ')}
        role="status"
        aria-label={label || 'Loading'}
      />
      {label && <span className="text-text-muted text-xs">{label}</span>}
    </div>
  )
}
