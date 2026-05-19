import { clsx } from "clsx";

export function SkeletonLoader({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={clsx("space-y-3", className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-4 bg-white/5 rounded animate-pulse" style={{ width: `${70 + Math.random() * 30}%` }} />
      ))}
    </div>
  );
}
