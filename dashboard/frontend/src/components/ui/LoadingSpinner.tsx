import { Loader2 } from "lucide-react";

export function LoadingSpinner({ label = "Loading..." }: { label?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-text-muted gap-3">
      <Loader2 className="w-6 h-6 animate-spin" />
      <span className="text-sm">{label}</span>
    </div>
  );
}
