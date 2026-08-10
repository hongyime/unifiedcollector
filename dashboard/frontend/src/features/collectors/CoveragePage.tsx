import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Database, ShieldAlert, Target } from "lucide-react";
import { api } from "../../services/api";
import type { CollectionCoverageRow } from "../../services/types";
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

export function CoveragePage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["collector-coverage"],
    queryFn: api.collectorsCoverage,
    refetchInterval: 60_000,
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
      <Header title="Collection Coverage" subtitle="Freshness, source gaps, private access failures, and rate-limit pressure" onRefresh={() => refetch()} />

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
    </div>
  );
}
