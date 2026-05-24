import { Loader2 } from 'lucide-react'

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
  loading?: boolean
  children: React.ReactNode
}

const VARIANTS = {
  primary: 'bg-white text-black hover:bg-white/90 font-medium',
  ghost: 'bg-transparent text-white border border-border hover:border-border-strong hover:bg-accent-5',
  danger: 'bg-transparent text-status-down border border-status-down/30 hover:bg-status-down/10',
}

const SIZES = {
  sm: 'px-3 py-1.5 text-xs',
  md: 'px-4 py-2 text-sm',
}

export default function Button({
  variant = 'ghost',
  size = 'md',
  loading = false,
  disabled,
  className = '',
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      disabled={disabled || loading}
      className={[
        'inline-flex items-center gap-2 rounded-md transition-colors duration-100 disabled:opacity-50 disabled:cursor-not-allowed',
        VARIANTS[variant],
        SIZES[size],
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...props}
    >
      {loading && <Loader2 size={12} className="animate-spin" />}
      {children}
    </button>
  )
}
