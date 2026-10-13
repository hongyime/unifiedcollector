import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Database, ShieldAlert, Target } from "lucide-react";
import { api } from "../../services/api";
import type {
  CollectionCoverageRow,
  CollectionSurfaceProgress,
  CollectorClassifiedError,
  CollectorFeedControl,
  CollectorQueueOperation,
} from "../../services/types";
import { Header } from "../../components/layout/Header";
import { DataTable } from "../../components/ui/DataTable";
import { ErrorState } from "../../components/ui/ErrorState";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { MetricCard } from "../../components/ui/MetricCard";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { formatNumber, relativeTime } from "../../utils/formatters";

function badgeStatus(status: string): "online" | "warning" | "error" | "idle" {
  if (status === "fresh") return "online";
  if (status === "degraded") return "warning";
  if (status === "stale") return "error";
  return "idle";
}

function staleTargetCount(value: CollectionCoverageRow["stale_targets"]): number {
  if (Array.isArray(value)) return value.length;
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return 0;
    const numeric = Number(trimmed);
    if (Number.isFinite(numeric)) return numeric;
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      return Array.isArray(parsed) ? parsed.length : 0;
    } catch {
      return 0;
    }
  }
  return 0;
}

const columns: ColumnDef<CollectionCoverageRow, unknown>[] = [
  {
    accessorKey: "source",
    header: "Source",
    cell: (info) => <span className="font-medium uppercase text-text-primary">{info.getValue() as string}</span>,
  },
  {
    accessorKey: "status",
    header: "Status",
    cell: (info) => {
      const status = info.getValue() as string;
      return <StatusBadge status={badgeStatus(status)} label={status} />;
    },
  },
  { accessorKey: "latest_data_at", header: "Latest data", cell: (info) => relativeTime(info.getValue() as string | null) },
  { accessorKey: "latest_run_at", header: "Latest run", cell: (info) => relativeTime(info.getValue() as string | null) },
  { accessorKey: "rows_24h", header: "Rows 24h", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "media_24h", header: "Media 24h", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "errors_24h", header: "Errors", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "rate_limits_24h", header: "429s", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "private_access_failures", header: "Private access", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "stale_targets", header: "Stale targets", cell: (info) => formatNumber(staleTargetCount(info.row.original.stale_targets)) },
];

const surfaceColumns: ColumnDef<CollectionSurfaceProgress, unknown>[] = [
  { accessorKey: "platform", header: "Platform", cell: (info) => <span className="font-medium uppercase text-text-primary">{info.getValue() as string}</span> },
  { accessorKey: "surface", header: "Surface", cell: (info) => {
    const value = String(info.getValue() || "unknown");
    return value === "for_you" ? "For You" : value === "message_stream" ? "Message stream" : value.replace(/_/g, " ");
  } },
  { accessorKey: "capture_count", header: "Captures", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "observed_count", header: "Observed", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "stored_count", header: "Stored", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "deduped_count", header: "Deduped", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "failed_count", header: "Failures", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "media_rows", header: "Media rows", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "message_rows", header: "Messages", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "last_success_at", header: "Last success", cell: (info) => relativeTime(info.getValue() as string | null) },
  { accessorKey: "freshness_seconds", header: "Freshness (s)", cell: (info) => info.getValue() == null ? "—" : formatNumber(info.getValue() as number) },
];

const queueColumns: ColumnDef<CollectorQueueOperation, unknown>[] = [
  { accessorKey: "platform", header: "Platform", cell: (info) => <span className="font-medium uppercase">{info.getValue() as string}</span> },
  { accessorKey: "pending_count", header: "Pending", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "in_progress_count", header: "In progress", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "failed_count", header: "Failed", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "oldest_pending_at", header: "Oldest pending", cell: (info) => relativeTime(info.getValue() as string | null) },
  { accessorKey: "oldest_pending_age_seconds", header: "Queue age (s)", cell: (info) => info.getValue() == null ? "—" : formatNumber(info.getValue() as number) },
];

const errorColumns: ColumnDef<CollectorClassifiedError, unknown>[] = [
  { accessorKey: "platform", header: "Platform", cell: (info) => <span className="font-medium uppercase">{info.getValue() as string}</span> },
  { accessorKey: "category", header: "Category", cell: (info) => <span className="capitalize">{String(info.getValue()).replace(/_/g, " ")}</span> },
  { accessorKey: "error_count", header: "Count", cell: (info) => formatNumber(info.getValue() as number) },
  { accessorKey: "source", header: "Source" },
  { accessorKey: "last_seen_at", header: "Last seen", cell: (info) => relativeTime(info.getValue() as string | null) },
];

export function CoveragePage() {
  const queryClient = useQueryClient();
  const [intervalEdits, setIntervalEdits] = useState<Record<string, number>>({});
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["collector-coverage"],
    queryFn: api.collectorsCoverage,
    refetchInterval: 60_000,
  });
  const surfaceQuery = useQuery({
    queryKey: ["collector-surface-progress"],
    queryFn: () => api.collectionSurfaceProgress(),
    refetchInterval: 60_000,
    retry: false,
  });
  const operationsQuery = useQuery({
    queryKey: ["collector-operations"],
    queryFn: () => api.collectorOperations(),
    refetchInterval: 60_000,
    retry: false,
  });
  const updateControl = useMutation({
    mutationFn: (control: { platform: string; enabled: boolean; interval_hours: number }) =>
      api.updateFeedControl(control.platform, control.enabled, control.interval_hours),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["collector-operations"] });
      void queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
  });

  const rows = data?.sources ?? [];
  const summary = useMemo(() => ({
    fresh: rows.filter((row) => row.status === "fresh").length,
    degraded: rows.filter((row) => row.status === "degraded").length,
    stale: rows.filter((row) => row.status === "stale").length,
    errors: rows.reduce((sum, row) => sum + row.errors_24h, 0),
    rateLimits: rows.reduce((sum, row) => sum + row.rate_limits_24h, 0),
    staleTargets: rows.reduce((sum, row) => sum + staleTargetCount(row.stale_targets), 0),
    media: rows.reduce((sum, row) => sum + row.media_24h, 0),
  }), [rows]);

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Collection Coverage" subtitle="Freshness, source gaps, private access failures, and rate-limit pressure" onRefresh={() => { void refetch(); void operationsQuery.refetch(); void surfaceQuery.refetch(); }} />

      <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Fresh sources"
          value={`${summary.fresh}/${rows.length}`}
          sublabel={`${formatNumber(summary.degraded)} degraded · ${formatNumber(summary.stale)} stale`}
          status={summary.stale || summary.degraded ? "warning" : "success"}
          icon={<Database className="h-5 w-5" />}
        />
        <MetricCard
          label="Media 24h"
          value={formatNumber(summary.media)}
          sublabel="rows contributing to analyzer evidence"
          status={summary.media > 0 ? "success" : "warning"}
          icon={<Database className="h-5 w-5" />}
        />
        <MetricCard
          label="Rate limits"
          value={formatNumber(summary.rateLimits)}
          sublabel={`${formatNumber(summary.errors)} total errors in 24h`}
          status={summary.rateLimits || summary.errors ? "warning" : "success"}
          icon={<ShieldAlert className="h-5 w-5" />}
        />
        <MetricCard
          label="Stale targets"
          value={formatNumber(summary.staleTargets)}
          sublabel="targets missing expected fresh data"
          status={summary.staleTargets ? "warning" : "success"}
          icon={<Target className="h-5 w-5" />}
        />
      </div>

      <div className="rounded-lg border border-border bg-surface p-4">
        <div className="mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text-muted">Source Gap Table</h2>
          <p className="mt-1 text-xs text-text-muted">
            Analyzer alert confidence should be discounted for degraded or stale sources.
          </p>
        </div>
        <DataTable data={rows} columns={columns} />
      </div>

      <div className="mt-6 rounded-lg border border-border bg-surface p-4">
        <div className="mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text-muted">Feed Surface Progress</h2>
          <p className="mt-1 text-xs text-text-muted">Browser captures, stored media, and native message streams by normalized surface over seven days.</p>
        </div>
        {surfaceQuery.data?.freshness_available === false && (
          <p className="mb-2 text-xs text-warning">Some freshness lookups failed; the listed rows may omit older surfaces.</p>
        )}
        {surfaceQuery.data?.available && surfaceQuery.data.surfaces.length > 0 ? (
          <DataTable data={surfaceQuery.data.surfaces} columns={surfaceColumns} />
        ) : (
          <p className="text-sm text-text-muted">
            {surfaceQuery.isLoading
              ? "Loading surface progress…"
              : !surfaceQuery.data
                ? "Surface progress is unavailable."
                : surfaceQuery.data.reason === "collection_surface_migration_pending"
                ? "Surface progress will appear after the collection-surface migration is applied."
                : surfaceQuery.data.freshness_available === false
                  ? "No recent surface events were returned, and all-time freshness lookup is unavailable."
                  : "No surface capture events in the last seven days."}
          </p>
        )}
      </div>

      <div className="mt-6 rounded-lg border border-border bg-surface p-4">
        <div className="mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text-muted">Spider Queue Age</h2>
          <p className="mt-1 text-xs text-text-muted">Age uses the oldest pending row in the shared queue. Legacy queue status remains in Backfill Equilibrium until shadow parity gates pass.</p>
        </div>
        {operationsQuery.data?.queue_age_available ? (
          <DataTable data={operationsQuery.data.queues} columns={queueColumns} />
        ) : (
          <p className="text-sm text-text-muted">
            {operationsQuery.isLoading ? "Loading queue age…" : "Shared queue age is unavailable; legacy queue consumers remain in place."}
          </p>
        )}
      </div>

      <div className="mt-6 rounded-lg border border-border bg-surface p-4">
        <div className="mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text-muted">Classified Collection Errors</h2>
          <p className="mt-1 text-xs text-text-muted">Recent browser, graph, and headless failures are grouped as auth, visibility, rate limit, timeout, parse, persistence, network, or other.</p>
        </div>
        {operationsQuery.data?.error_reporting_partial && (
          <p className="mb-2 text-xs text-warning">Some error sources are unavailable; results may be incomplete.</p>
        )}
        {operationsQuery.isLoading ? (
          <p className="text-sm text-text-muted">Loading error classifications…</p>
        ) : !operationsQuery.data?.error_reporting_available ? (
          <p className="text-sm text-text-muted">Error reporting is unavailable; no error source could be queried.</p>
        ) : operationsQuery.data.errors.length ? (
          <DataTable data={operationsQuery.data.errors} columns={errorColumns} />
        ) : operationsQuery.data.error_reporting_partial ? (
          <p className="text-sm text-text-muted">No classified errors were returned from available sources; other sources could not be queried.</p>
        ) : (
          <p className="text-sm text-text-muted">No classified browser, graph, or headless errors in the last {operationsQuery.data.hours} hours.</p>
        )}
      </div>

      <div className="mt-6 rounded-lg border border-border bg-surface p-4">
        <div className="mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text-muted">Per-platform pause and cadence</h2>
          <p className="mt-1 text-xs text-text-muted">Pause disables scheduled work and the browser extension on its next config poll. Interval sets the headless scheduler cadence (1–720 hours); hard page/item ceilings remain in collector config.</p>
        </div>
        {operationsQuery.data ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-border text-left text-text-muted"><th className="pb-2">Platform</th><th className="pb-2">Enabled</th><th className="pb-2">Interval (h)</th><th className="pb-2">Next run</th><th className="pb-2" /></tr></thead>
              <tbody>
                {operationsQuery.data.controls.map((control: CollectorFeedControl) => {
                  const interval = intervalEdits[control.platform] ?? control.interval_hours;
                  const save = (enabled = control.enabled) => updateControl.mutate({
                    platform: control.platform,
                    enabled,
                    interval_hours: Math.max(1, Math.min(720, interval || 24)),
                  });
                  return (
                    <tr key={control.platform} className="border-b border-border/50">
                      <td className="py-2 font-medium uppercase">{control.platform}</td>
                      <td className="py-2">
                        <button
                          type="button"
                          onClick={() => save(!control.enabled)}
                          disabled={updateControl.isPending}
                          className={`rounded px-2 py-1 text-xs ${control.enabled ? "bg-success/20 text-success" : "bg-warning/20 text-warning"}`}
                        >{control.enabled ? "Pause" : "Resume"}</button>
                      </td>
                      <td className="py-2">
                        <input
                          type="number"
                          min={1}
                          max={720}
                          value={interval}
                          onChange={(event) => setIntervalEdits({ ...intervalEdits, [control.platform]: Number(event.target.value) })}
                          className="w-20 rounded-md border border-border bg-background px-2 py-1"
                        />
                      </td>
                      <td className="py-2 text-text-muted">{relativeTime(control.next_run)}</td>
                      <td className="py-2">
                        {interval !== control.interval_hours && (
                          <button type="button" onClick={() => save()} disabled={updateControl.isPending} className="text-xs text-info hover:underline">Save</button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-text-muted">{operationsQuery.isLoading ? "Loading collector controls…" : "Collector controls are unavailable."}</p>
        )}
        {updateControl.error && <p className="mt-2 text-xs text-error">Control update failed: {String(updateControl.error)}</p>}
      </div>
    </div>
  );
}
