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

const valueTone: Record<string, string> = {
  success: "text-text-primary",
  error: "text-error",
  warning: "text-warning",
  info: "text-text-primary",
  idle: "text-text-primary",
};

export function MetricCard({ label, value, sublabel, icon, status }: MetricCardProps) {
  return (
    <div className="group relative overflow-hidden rounded-xl border border-border-strong bg-surface-2 p-4 transition-colors hover:border-white/20">
      {status && <span className={clsx("absolute inset-x-0 top-0 h-0.5", dots[status])} />}
      <div className="flex items-start justify-between gap-2">
        <p className="text-[0.7rem] font-medium uppercase tracking-widest text-text-secondary">{label}</p>
        <div className="flex items-center gap-2">
          {status && <div className={clsx("h-2 w-2 rounded-full", dots[status])} />}
          {icon && <div className="text-text-muted">{icon}</div>}
        </div>
      </div>
      <p className={clsx("mt-2 font-mono text-3xl font-semibold leading-none tabular-nums", status ? valueTone[status] : "text-text-primary")}>
        {value}
      </p>
      {sublabel && <p className="mt-1.5 text-xs text-text-muted">{sublabel}</p>}
    </div>
  );
}
