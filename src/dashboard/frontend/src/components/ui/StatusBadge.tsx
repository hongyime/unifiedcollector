import { clsx } from "clsx";

type Status = "online" | "offline" | "warning" | "error" | "processing" | "idle";

interface StatusBadgeProps {
  status: Status;
  label?: string;
}

const cfg: Record<Status, { dot: string; text: string; fallback: string }> = {
  online:     { dot: "bg-success",                text: "text-success",      fallback: "Online" },
  offline:    { dot: "bg-text-muted",             text: "text-text-muted",   fallback: "Offline" },
  warning:    { dot: "bg-warning",                text: "text-warning",      fallback: "Warning" },
  error:      { dot: "bg-error",                  text: "text-error",        fallback: "Error" },
  processing: { dot: "bg-info animate-pulse",     text: "text-info",         fallback: "Processing" },
  idle:       { dot: "bg-text-muted",             text: "text-text-muted",   fallback: "Idle" },
};

export function StatusBadge({ status, label }: StatusBadgeProps) {
  const c = cfg[status];
  return (
    <span className={clsx("inline-flex items-center gap-1.5 text-xs", c.text)}>
      <span className={clsx("w-2 h-2 rounded-full", c.dot)} />
      {label ?? c.fallback}
    </span>
  );
}
