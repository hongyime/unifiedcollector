import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { FilterDropdown } from "../../components/ui/FilterDropdown";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { formatDuration, formatNumber, relativeTime } from "../../utils/formatters";
import { SOURCES } from "../../utils/constants";
import type { Run } from "../../services/types";

const sourceOptions = [
  { value: "", label: "All sources" },
  ...SOURCES.map((s) => ({ value: s, label: s.charAt(0).toUpperCase() + s.slice(1) })),
];

const statusColor: Record<string, string> = { completed: "bg-success", running: "bg-warning", failed: "bg-error" };

function runStoredSummary(run: Run) {
  const ingestion = run.ingestion;
  if (!ingestion) return "—";
  return `${formatNumber(ingestion.records)} rows · ${formatNumber(ingestion.media_items)} files`;
}

function runIngestedCount(run: Run) {
  if (run.ingestion_items != null) return run.ingestion_items;
  const ingestion = run.ingestion;
  if (!ingestion) return run.items_collected;
  return ingestion.records + ingestion.media_items + ingestion.rate_limits + ingestion.access_errors;
}

function runHttpSummary(run: Run) {
  const ingestion = run.ingestion;
  if (!ingestion) return "—";
  const total = ingestion.rate_limits + ingestion.access_errors;
  if (!total) return <span className="text-text-muted">0</span>;
  return (
    <span className={ingestion.access_errors ? "text-error" : "text-warning"}>
      {formatNumber(ingestion.rate_limits)} 429 · {formatNumber(ingestion.access_errors)} auth/other
    </span>
  );
}

export function RunsPage() {
  const [source, setSource] = useState("");
  const [selected, setSelected] = useState<Run | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["runs", source],
    queryFn: () => api.runs(source || undefined),
  });

  const detail = useQuery({
    queryKey: ["run-detail", selected?.id],
    queryFn: () => api.runDetail(selected!.id),
    enabled: !!selected,
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Runs" subtitle="Scheduler run windows with real stored rows, files, messages, and HTTP pressure" onRefresh={() => refetch()} />
      <div className="flex items-center gap-3 mb-4">
        <FilterDropdown label="Source" value={source} onChange={setSource} options={sourceOptions} />
      </div>
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">ID</th><th className="pb-2">Source</th><th className="pb-2">Status</th><th className="pb-2">Started</th><th className="pb-2">Ingested</th><th className="pb-2">Stored</th><th className="pb-2">Messages</th><th className="pb-2">HTTP</th><th className="pb-2">Errors</th>
            </tr></thead>
            <tbody>
              {data?.map((r) => (
                <tr key={r.id} onClick={() => setSelected(r)} className="border-b border-border/50 hover:bg-white/5 cursor-pointer">
                  <td className="py-2">{r.id}</td>
                  <td className="py-2 uppercase text-xs">{r.source}</td>
                  <td className="py-2"><span className={`inline-flex items-center gap-1.5 text-xs`}><span className={`w-2 h-2 rounded-full ${statusColor[r.status] ?? "bg-text-muted"}`} />{r.status}</span></td>
                  <td className="py-2 text-text-muted">{relativeTime(r.started_at)}</td>
                  <td className="py-2">{formatNumber(runIngestedCount(r))}</td>
                  <td className="py-2">{runStoredSummary(r)}</td>
                  <td className="py-2">{formatNumber(r.ingestion?.messages ?? 0)}</td>
                  <td className="py-2">{runHttpSummary(r)}</td>
                  <td className="py-2">{r.errors > 0 ? <span className="text-error">{r.errors}</span> : 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {selected && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setSelected(null)}>
          <div className="bg-surface border border-border rounded-lg p-6 max-w-lg w-full" onClick={(e) => e.stopPropagation()}>
            <h2 className="text-lg font-semibold mb-4">Run #{selected.id}</h2>
            {detail.isLoading ? <LoadingSpinner /> : detail.data ? (
              <dl className="grid grid-cols-2 gap-2 text-sm">
                <dt className="text-text-muted">Source</dt><dd className="uppercase">{detail.data.source}</dd>
                <dt className="text-text-muted">Status</dt><dd>{detail.data.status}</dd>
                <dt className="text-text-muted">Started</dt><dd>{relativeTime(detail.data.started_at)}</dd>
                <dt className="text-text-muted">Finished</dt><dd>{relativeTime(detail.data.finished_at)}</dd>
                <dt className="text-text-muted">Window</dt><dd>{detail.data.ingestion?.window_seconds == null ? "—" : formatDuration(detail.data.ingestion.window_seconds)}</dd>
                <dt className="text-text-muted">Count basis</dt><dd>{detail.data.ingestion?.exact_window ? "exact run window" : "source-hour bucket"}</dd>
                <dt className="text-text-muted">Total ingested</dt><dd>{formatNumber(runIngestedCount(detail.data))}</dd>
                <dt className="text-text-muted">Stored rows</dt><dd>{formatNumber(detail.data.ingestion?.records ?? 0)}</dd>
                <dt className="text-text-muted">Messages</dt><dd>{formatNumber(detail.data.ingestion?.messages ?? 0)}</dd>
                <dt className="text-text-muted">Media files</dt><dd>{formatNumber(detail.data.ingestion?.media_items ?? 0)}</dd>
                <dt className="text-text-muted">HTTP pressure</dt><dd>{runHttpSummary(detail.data)}</dd>
                <dt className="text-text-muted">Latest stored row</dt><dd>{relativeTime(detail.data.ingestion?.latest_at ?? null)}</dd>
                <dt className="text-text-muted">Scheduler targets rearmed</dt><dd>{formatNumber(detail.data.items_collected)}</dd>
                <dt className="text-text-muted">Errors</dt><dd>{detail.data.errors}</dd>
              </dl>
            ) : null}
            <button onClick={() => setSelected(null)} className="mt-4 text-sm text-text-muted hover:text-text-primary">Close</button>
          </div>
        </div>
      )}
    </div>
  );
}
