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
import type { MediaStats, MessagingCoverageRow } from "../../services/types";

function liveBadgeStatus(status: MediaStats["live"]) {
  if (status === "live") return "online";
  if (status === "stale" || status === "degraded") return "warning";
  if (status === "dead") return "error";
  return "idle";
}

const columns: ColumnDef<MediaStats, unknown>[] = [
  {
    accessorKey: "source",
    header: "Source",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div className="flex items-center gap-2">
          <StatusBadge status={liveBadgeStatus(row.live)} label={row.live ?? "unknown"} />
          <span className="uppercase font-medium text-text-primary">{info.getValue() as string}</span>
        </div>
      );
    },
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
    accessorKey: "last_activity",
    header: "Activity",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div>
          <div>{relativeTime((info.getValue() as string | null) ?? row.last_collected)}</div>
          {row.activity_basis && (
            <div className="text-[10px] uppercase tracking-wide text-text-muted">
              {row.activity_basis}
            </div>
          )}
        </div>
      );
    },
  },
  {
    accessorKey: "last_collected",
    header: "Media",
    cell: (info) => relativeTime(info.getValue() as string | null),
  },
];

const messagingColumns: ColumnDef<MessagingCoverageRow, unknown>[] = [
  {
    accessorKey: "network",
    header: "Network",
    cell: (info) => <span className="font-medium">{info.getValue() as string}</span>,
  },
  {
    accessorKey: "canonical_source",
    header: "Canonical",
    cell: (info) => {
      const value = info.getValue() as MessagingCoverageRow["canonical_source"];
      return <StatusBadge status={value === "native" ? "online" : "processing"} label={value} />;
    },
  },
  {
    accessorKey: "native_messages",
    header: "Native",
    cell: (info) => {
      const row = info.row.original;
      if (!row.native_source) return <span className="text-text-muted">—</span>;
      return (
        <div>
          <div>{formatNumber(info.getValue() as number)} msgs</div>
          <div className="text-[10px] uppercase tracking-wide text-text-muted">
            {formatNumber(row.native_chats)} chats
          </div>
        </div>
      );
    },
  },
  {
    accessorKey: "beeper_messages",
    header: "Beeper",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div>
          <div>{formatNumber(info.getValue() as number)} msgs</div>
          <div className="text-[10px] uppercase tracking-wide text-text-muted">
            {formatNumber(row.beeper_chats)} chats · {row.policy}
          </div>
        </div>
      );
    },
  },
  {
    accessorKey: "beeper_last_message",
    header: "Last Seen",
    cell: (info) => {
      const row = info.row.original;
      return relativeTime((row.native_last_message ?? info.getValue()) as string | null);
    },
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

  const { data: messagingCoverage } = useQuery({
    queryKey: ["messaging-coverage"],
    queryFn: api.messagingCoverage,
    refetchInterval: 60_000,
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

      <div className="bg-surface rounded-lg border border-border p-4 mt-6">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-4">Messaging Coverage</h2>
        <DataTable data={messagingCoverage ?? []} columns={messagingColumns} />
      </div>
    </div>
  );
}
