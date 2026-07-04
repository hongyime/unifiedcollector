import { useQuery } from "@tanstack/react-query";
import { Header } from "../../components/layout/Header";
import { MetricCard } from "../../components/ui/MetricCard";
import { DataTable } from "../../components/ui/DataTable";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { api } from "../../services/api";
import { formatBytes, formatNumber, relativeTime } from "../../utils/formatters";
import { Database, HardDrive, Activity, AlertCircle } from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type { MediaStats } from "../../services/types";

const columns: ColumnDef<MediaStats, unknown>[] = [
  {
    accessorKey: "source",
    header: "Source",
    cell: (info) => <span className="uppercase font-medium text-text-primary">{info.getValue() as string}</span>,
  },
  {
    accessorKey: "total_items",
    header: "Items",
    cell: (info) => formatNumber(info.getValue() as number),
  },
  {
    accessorKey: "total_bytes",
    header: "Size",
    cell: (info) => formatBytes(info.getValue() as number),
  },
  {
    accessorKey: "last_collected",
    header: "Last Collected",
    cell: (info) => relativeTime(info.getValue() as string | null),
  },
];

export function DashboardPage() {
  const { data: health, isLoading: hLoading } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 5_000,
  });

  const { data: stats, isLoading: sLoading, error, refetch } = useQuery({
    queryKey: ["media-stats"],
    queryFn: api.mediaStats,
    refetchInterval: 30_000,
  });

  const { data: collectorsLive } = useQuery({
    queryKey: ["collectors-live"],
    queryFn: api.collectorsLive,
    refetchInterval: 15_000,
  });

  if (hLoading && sLoading) return <LoadingSpinner />;
  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  const totalItems = stats?.reduce((s, r) => s + r.total_items, 0) ?? 0;
  const totalBytes = stats?.reduce((s, r) => s + r.total_bytes, 0) ?? 0;
  // Real liveness from /collectors/live (data freshness + source_health), not the
  // service_cursors.status proxy that flickered for healthy idle/realtime collectors.
  const liveCollectors = collectorsLive?.live ?? 0;
  const totalCollectors = collectorsLive?.total ?? 0;

  return (
    <div>
      <Header title="Dashboard" subtitle="System overview" onRefresh={() => refetch()} />

      <div className="grid grid-cols-4 gap-3 mb-6">
        <MetricCard
          label="System"
          value={health?.status === "ok" ? "Healthy" : "Degraded"}
          status={health?.status === "ok" ? "success" : "warning"}
          icon={<AlertCircle className="w-5 h-5" />}
        />
        <MetricCard
          label="Total Items"
          value={formatNumber(totalItems)}
          icon={<Database className="w-5 h-5" />}
        />
        <MetricCard
          label="Total Size"
          value={formatBytes(totalBytes)}
          icon={<HardDrive className="w-5 h-5" />}
        />
        <MetricCard
          label="Collectors"
          value={`${liveCollectors} / ${totalCollectors}`}
          sublabel={liveCollectors === totalCollectors ? "all live" : "live"}
          status={liveCollectors === totalCollectors ? "success" : liveCollectors > 0 ? "warning" : "idle"}
          icon={<Activity className="w-5 h-5" />}
        />
      </div>

      <div className="grid grid-cols-2 gap-3 mb-6">
        <MetricCard
          label="Database"
          value={health?.database === "healthy" ? "OK" : "Error"}
          status={health?.database === "healthy" ? "success" : "error"}
        />
        <MetricCard
          label="Drive"
          value={health?.drive === "mounted" ? "Mounted" : "Missing"}
          status={health?.drive === "mounted" ? "success" : "error"}
        />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-4">Collection Stats by Source</h2>
        {sLoading ? <LoadingSpinner /> : <DataTable data={stats ?? []} columns={columns} />}
      </div>
    </div>
  );
}
