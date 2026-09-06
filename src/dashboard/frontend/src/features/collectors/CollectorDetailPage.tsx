import { useParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { MetricCard } from "../../components/ui/MetricCard";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { formatNumber, relativeTime } from "../../utils/formatters";

export function CollectorDetailPage() {
  const { source } = useParams<{ source: string }>();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["collector-detail", source],
    queryFn: () => api.collectorDetail(source!),
    enabled: !!source,
  });

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;
  if (!data) return null;

  const cursorStatus = data.cursor?.status ?? "unknown";

  return (
    <div>
      <Header title={source?.toUpperCase() ?? "Collector"} subtitle="Collector detail" onRefresh={() => refetch()} />
      <div className="grid grid-cols-3 gap-4 mb-6">
        <MetricCard label="Media Count" value={formatNumber(data.media_count)} status="info" />
        <MetricCard label="Error Count" value={formatNumber(data.error_count)} status={data.error_count > 0 ? "error" : "success"} />
        <div className="bg-surface rounded-lg border border-border p-4">
          <p className="text-xs uppercase tracking-wider text-text-muted">Cursor Status</p>
          <div className="mt-2">
            <StatusBadge status={cursorStatus === "running" ? "processing" : cursorStatus === "completed" ? "online" : "idle"} label={cursorStatus} />
          </div>
          {data.cursor?.last_processed_at && (
            <p className="text-xs text-text-muted mt-1">Last: {relativeTime(data.cursor.last_processed_at)}</p>
          )}
          {data.cursor?.last_processed_id && (
            <p className="text-xs text-text-muted mt-0.5 truncate">ID: {data.cursor.last_processed_id}</p>
          )}
        </div>
      </div>
      <div className="bg-surface rounded-lg border border-border p-4">
        <h2 className="text-sm font-semibold mb-3">Recent Items</h2>
        <table className="w-full text-sm">
          <thead><tr className="text-left text-text-muted border-b border-border">
            <th className="pb-2">Filename</th><th className="pb-2">Entity</th><th className="pb-2">Type</th><th className="pb-2">Collected</th>
          </tr></thead>
          <tbody>
            {data.recent_items.map((item) => (
              <tr key={item.id} className="border-b border-border/50 hover:bg-white/5">
                <td className="py-2 truncate max-w-[200px]">{item.filename}</td>
                <td className="py-2">{item.entity_name}</td>
                <td className="py-2 text-xs text-text-muted">{item.content_type}</td>
                <td className="py-2 text-text-muted">{relativeTime(item.collected_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
