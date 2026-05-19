import { clsx } from "clsx";
import type { ReactNode } from "react";

interface MetricCardProps {
  label: string;
  value: string | number;
  sublabel?: string;
  icon?: ReactNode;
  status?: "success" | "error" | "warning" | "info" | "idle";
}

const dots: Record<string, string> = {
  success: "bg-success",
  error: "bg-error",
  warning: "bg-warning",
  info: "bg-info",
  idle: "bg-text-muted",
};

export function MetricCard({ label, value, sublabel, icon, status }: MetricCardProps) {
  return (
    <div className="bg-surface rounded-lg border border-border p-4">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs uppercase tracking-wider text-text-muted">{label}</p>
          <p className="mt-1 text-2xl font-semibold font-mono">{value}</p>
          {sublabel && <p className="mt-1 text-xs text-text-muted">{sublabel}</p>}
        </div>
        <div className="flex items-center gap-2">
          {status && <div className={clsx("w-2 h-2 rounded-full", dots[status])} />}
          {icon && <div className="text-text-secondary">{icon}</div>}
        </div>
      </div>
    </div>
  );
}
