import { RefreshCw } from "lucide-react";
import { Button } from "../ui/Button";
import type { ReactNode } from "react";

interface HeaderProps {
  title: string;
  subtitle?: string;
  onRefresh?: () => void;
  actions?: ReactNode;
}

export function Header({ title, subtitle, onRefresh, actions }: HeaderProps) {
  return (
    <div className="flex items-center justify-between mb-6">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {subtitle && <p className="text-sm text-text-secondary mt-0.5">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-3">
        {actions}
        {onRefresh && (
          <Button variant="ghost" size="sm" onClick={onRefresh} icon={<RefreshCw className="w-3.5 h-3.5" />}>
            Refresh
          </Button>
        )}
      </div>
    </div>
  );
}
