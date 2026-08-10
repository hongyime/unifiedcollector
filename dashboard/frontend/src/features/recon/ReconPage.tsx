import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Radar, ShieldAlert, Target } from "lucide-react";
import { api } from "../../services/api";
import type { ReconTarget } from "../../services/types";
import { Header } from "../../components/layout/Header";
import { DataTable } from "../../components/ui/DataTable";
import { ErrorState } from "../../components/ui/ErrorState";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { MetricCard } from "../../components/ui/MetricCard";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { formatNumber, relativeTime } from "../../utils/formatters";

function statusKind(status: string): "online" | "processing" | "warning" | "error" | "idle" {
  if (status === "completed") return "online";
  if (status === "running") return "processing";
  if (status === "failed" || status === "timeout" || status === "blocked") return "error";
  if (status === "pending") return "warning";
  return "idle";
}

function hasScope(value: ReconTarget["scope_json"]): boolean {
  if (!value) return false;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value) as unknown;
      return Boolean(parsed && typeof parsed === "object" && !Array.isArray(parsed) && Object.keys(parsed).length > 0);
    } catch {
      return value.trim().length > 0 && value.trim() !== "{}";
    }
  }
  return Object.keys(value).length > 0;
}

const columns: ColumnDef<ReconTarget, unknown>[] = [
  {
    accessorKey: "target_type",
    header: "Type",
    cell: (info) => <span className="uppercase text-xs text-text-secondary">{info.getValue() as string}</span>,
  },
  {
    accessorKey: "target_value",
    header: "Target",
    cell: (info) => <span className="font-mono text-sm text-text-primary">{info.getValue() as string}</span>,
  },
  { accessorKey: "source", header: "Source" },
  { accessorKey: "priority", header: "Priority", cell: (info) => formatNumber(info.getValue() as number) },
  {
    accessorKey: "status",
    header: "Status",
    cell: (info) => {
      const status = info.getValue() as string;
      return <StatusBadge status={statusKind(status)} label={status} />;
    },
  },
  { accessorKey: "updated_at", header: "Updated", cell: (info) => relativeTime(info.getValue() as string | null) },
  {
    accessorKey: "error",
    header: "Error",
    cell: (info) => <span className="block max-w-[260px] truncate text-xs text-error">{(info.getValue() as string | null) ?? ""}</span>,
  },
];

export function ReconPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["recon-targets"],
    queryFn: api.reconTargets,
    refetchInterval: 60_000,
  });

  const rows = data?.targets ?? [];
  const summary = useMemo(() => ({
    pending: rows.filter((row) => row.status === "pending").length,
    running: rows.filter((row) => row.status === "running").length,
    completed: rows.filter((row) => row.status === "completed").length,
    failed: rows.filter((row) => ["failed", "timeout", "blocked"].includes(row.status)).length,
    scoped: rows.filter((row) => hasScope(row.scope_json)).length,
  }), [rows]);

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Recon" subtitle="SpiderFoot-style sidecar queue. Findings are weak leads only." onRefresh={() => refetch()} />

      <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Queued"
          value={formatNumber(summary.pending)}
          sublabel={`${formatNumber(summary.running)} running`}
          status={summary.pending || summary.running ? "warning" : "idle"}
          icon={<Target className="h-5 w-5" />}
        />
        <MetricCard
          label="Completed"
          value={formatNumber(summary.completed)}
          sublabel="normalized into recon observations"
          status={summary.completed ? "success" : "idle"}
          icon={<Radar className="h-5 w-5" />}
        />
        <MetricCard
          label="Failed or blocked"
          value={formatNumber(summary.failed)}
          sublabel="scope guard, missing sidecar, timeout, or module failure"
          status={summary.failed ? "error" : "success"}
          icon={<ShieldAlert className="h-5 w-5" />}
        />
        <MetricCard
          label="Scoped"
          value={formatNumber(summary.scoped)}
          sublabel="targets carrying explicit scope metadata"
          status="info"
          icon={<ShieldAlert className="h-5 w-5" />}
        />
      </div>

      <div className="rounded-lg border border-border bg-surface p-4">
        <div className="mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text-muted">Target Queue</h2>
          <p className="mt-1 text-xs text-text-muted">
            Domains, URLs, emails, usernames, and IPs can be queued. Recon output must stay weak evidence until reviewed elsewhere.
          </p>
        </div>
        <DataTable data={rows} columns={columns} />
      </div>
    </div>
  );
}
