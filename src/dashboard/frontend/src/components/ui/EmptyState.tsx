import { Inbox } from "lucide-react";
import type { ReactNode } from "react";

interface EmptyStateProps {
  icon?: ReactNode;
  title?: string;
  description?: string;
}

export function EmptyState({
  icon = <Inbox className="w-10 h-10" />,
  title = "No data yet",
  description = "Nothing to display.",
}: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-text-muted gap-3">
      {icon}
      <p className="text-sm font-medium">{title}</p>
      <p className="text-xs">{description}</p>
    </div>
  );
}
