export default function LoadingSpinner({ className = '', label }: { className?: string; label?: string }) {
  return (
    <div className={`flex flex-col items-center justify-center gap-3 ${className}`}>
      <div className="w-6 h-6 rounded-full border-2 border-white/20 border-t-white/80 animate-spin" />
      {label && <span className="text-text-muted text-sm">{label}</span>}
    </div>
  )
}
