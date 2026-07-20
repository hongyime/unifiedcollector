import { useQuery } from "@tanstack/react-query";
import { Header } from "../../components/layout/Header";
import { MetricCard } from "../../components/ui/MetricCard";
import { DataTable } from "../../components/ui/DataTable";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { api } from "../../services/api";
import { formatBytes, formatNumber, relativeTime } from "../../utils/formatters";
import { Database, HardDrive, Activity, AlertCircle, Clock3, ShieldAlert } from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type {
  HourlyIngestionRow,
  MediaStats,
  MessagingCoverageRow,
  RateLimitEvent,
  RateLimitRecentSummary,
} from "../../services/types";

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
      const row = info.row.original;
      const value = info.getValue() as MessagingCoverageRow["canonical_source"];
      return (
        <div>
          <StatusBadge status={value === "native" ? "online" : "processing"} label={value} />
          <div className="text-[10px] uppercase tracking-wide text-text-muted mt-1">{row.policy}</div>
        </div>
      );
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
            {formatNumber(row.native_chats)} chats · {formatNumber(row.native_people)} people
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
            {formatNumber(row.beeper_chats)} chats · {formatNumber(row.beeper_people)} people
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

const hourlyColumns: ColumnDef<HourlyIngestionRow, unknown>[] = [
  {
    accessorKey: "hour",
    header: "Hour",
    cell: (info) => new Date(info.getValue() as string).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
  },
  {
    accessorKey: "source",
    header: "Source",
    cell: (info) => <span className="uppercase font-medium">{info.getValue() as string}</span>,
  },
  {
    accessorKey: "records",
    header: "Rows",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div>
          <div>{formatNumber(info.getValue() as number)}</div>
          <div className="text-[10px] uppercase tracking-wide text-text-muted">{row.record_label}</div>
        </div>
      );
    },
  },
  {
    accessorKey: "messages",
    header: "Msgs",
    cell: (info) => formatNumber(info.getValue() as number),
  },
  {
    accessorKey: "media_items",
    header: "Files",
    cell: (info) => formatNumber(info.getValue() as number),
  },
  {
    accessorKey: "rate_limits",
    header: "429s",
    cell: (info) => {
      const value = info.getValue() as number;
      return value > 0 ? <span className="text-warning font-semibold">{value}</span> : <span className="text-text-muted">0</span>;
    },
  },
];

function formatCooldown(seconds: number | null | undefined) {
  if (!seconds) return "-";
  const mins = Math.round(seconds / 60);
  if (mins >= 60) return `${(mins / 60).toFixed(1)}h`;
  return `${mins}m`;
}

function formatClock(iso: string | null | undefined) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function normalizeRateLimitSource(value: string) {
  return value
    .toLowerCase()
    .replace(/_?rate_?limit/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function formatRateLimitService(service: string) {
  const label = normalizeRateLimitSource(service);
  return label || service.replace(/_/g, " ");
}

function formatRateLimitSummaryLabel(row: RateLimitRecentSummary) {
  const parts = [row.source, row.account, row.scope]
    .filter((part): part is string => Boolean(part))
    .map((part) => part.replace(/_/g, " "));
  return parts.join(" · ");
}

const rateLimitColumns: ColumnDef<RateLimitEvent, unknown>[] = [
  {
    accessorKey: "created_at",
    header: "When",
    cell: (info) => relativeTime(info.getValue() as string),
  },
  {
    accessorKey: "source",
    header: "Source",
    cell: (info) => <span className="uppercase font-medium">{info.getValue() as string}</span>,
  },
  {
    accessorKey: "account",
    header: "Account",
    cell: (info) => (info.getValue() as string | null) ?? "—",
  },
  {
    accessorKey: "scope",
    header: "Scope",
    cell: (info) => (info.getValue() as string | null) ?? "—",
  },
  {
    accessorKey: "cooldown_seconds",
    header: "Cooldown",
    cell: (info) => formatCooldown(info.getValue() as number | null),
  },
  {
    accessorKey: "reason",
    header: "Reason",
    cell: (info) => <span className="text-text-muted">{(info.getValue() as string | null) ?? "429"}</span>,
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

  const { data: hourly } = useQuery({
    queryKey: ["ingestion-hourly", 12],
    queryFn: () => api.hourlyIngestion(12),
    refetchInterval: 60_000,
  });

  const { data: rateLimits } = useQuery({
    queryKey: ["rate-limits", 24],
    queryFn: () => api.rateLimits(24, 80),
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
  const newestHour = hourly?.[0]?.hour;
  const currentHourRows = hourly?.filter((r) => r.hour === newestHour) ?? [];
  const currentRows = currentHourRows.reduce((s, r) => s + r.records, 0);
  const currentMessages = currentHourRows.reduce((s, r) => s + r.messages, 0);
  const currentFiles = currentHourRows.reduce((s, r) => s + r.media_items, 0);
  const nowMs = Date.now();
  const activeCursorLimits = (rateLimits?.active ?? []).filter((r) => {
    const expiryMs = r.active_until ? new Date(r.active_until).getTime() : Number.NaN;
    return r.status === "blocked" && (!r.active_until || Number.isNaN(expiryMs) || expiryMs > nowMs);
  });
  const activeCursorSources = new Set(activeCursorLimits.map((r) => formatRateLimitService(r.service)));
  const recentLimitSummaries = rateLimits?.recent_summary ?? [];
  const activeEventLimits = recentLimitSummaries.filter(
    (r) => r.active_now && !activeCursorSources.has(normalizeRateLimitSource(r.source)),
  );
  const recentRateLimitEvents = rateLimits?.events.length ?? 0;
  const recentRateLimitScopes = recentLimitSummaries.length;
  const activeRateLimits = activeCursorLimits.length + activeEventLimits.length;
  const vault = health?.vault;
  const vaultOk = vault?.available && vault?.writable;
  const vaultIssues = (vault?.artifacts_queued ?? 0) + (vault?.artifacts_partial ?? 0);

  return (
    <div>
      <Header title="Dashboard" subtitle="System overview" onRefresh={() => refetch()} />

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3 mb-6">
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

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3 mb-6">
        <MetricCard
          label="Database"
          value={health?.database === "healthy" ? "OK" : "Error"}
          status={health?.database === "healthy" ? "success" : "error"}
        />
        <MetricCard
          label="Vault"
          value={vaultOk ? "Writable" : "Blocked"}
          sublabel={
            vault?.free_bytes != null
              ? `${formatBytes(vault.free_bytes)} free · ${formatNumber(vaultIssues)} issues`
              : health?.drive ?? "unknown"
          }
          status={vaultOk && vaultIssues === 0 ? "success" : vaultOk ? "warning" : "error"}
        />
        <MetricCard
          label="This Hour"
          value={formatNumber(currentRows)}
          sublabel={`${formatNumber(currentMessages)} msgs · ${formatNumber(currentFiles)} files`}
          status={currentRows > 0 || currentFiles > 0 ? "success" : "warning"}
          icon={<Clock3 className="w-5 h-5" />}
        />
        <MetricCard
          label="Rate Limits"
          value={activeRateLimits ? `${activeRateLimits} active` : `${recentRateLimitScopes}`}
          sublabel={
            activeRateLimits
              ? `${formatNumber(recentRateLimitEvents)} 429s last 24h`
              : "source/account scopes last 24h"
          }
          status={activeRateLimits || recentRateLimitEvents ? "warning" : "success"}
          icon={<ShieldAlert className="w-5 h-5" />}
        />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4 mb-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-xs uppercase tracking-wider text-text-muted">Hourly Ingestion</h2>
            <p className="text-xs text-text-muted mt-1">Real rows/files by hour. This is the early-warning view; run history is only scheduler re-arms.</p>
          </div>
        </div>
        <DataTable data={(hourly ?? []).slice(0, 80)} columns={hourlyColumns} />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4 mb-6">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-4">Rate Limits</h2>
        {(activeCursorLimits.length > 0 || activeEventLimits.length > 0) && (
          <div className="mb-3 grid grid-cols-1 md:grid-cols-2 gap-2">
            {activeCursorLimits.map((r) => (
              <div key={r.service} className="bg-warning/10 border border-warning/30 rounded-md px-3 py-2 text-xs">
                <div className="font-medium text-warning capitalize">{formatRateLimitService(r.service)}</div>
                <div className="text-text-muted">
                  {r.active_until ? `cooldown until ${formatClock(r.active_until)}` : r.status}
                  {r.streak ? ` · streak ${r.streak}` : ""}
                </div>
              </div>
            ))}
            {activeEventLimits.map((r) => (
              <div
                key={`${r.source}-${r.account ?? ""}-${r.scope ?? ""}`}
                className="bg-warning/10 border border-warning/30 rounded-md px-3 py-2 text-xs"
              >
                <div className="font-medium text-warning capitalize">{formatRateLimitSummaryLabel(r)}</div>
                <div className="text-text-muted">
                  {r.active_until ? `cooldown until ${formatClock(r.active_until)}` : "cooldown observed"}
                  {` · ${formatNumber(r.count)} 429s`}
                </div>
              </div>
            ))}
          </div>
        )}
        {recentLimitSummaries.length > 0 && (
          <div className="mb-4">
            <div className="text-[10px] uppercase tracking-wide text-text-muted mb-2">Recent 429 scopes</div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
              {recentLimitSummaries.slice(0, 8).map((r) => (
                <div key={`${r.source}-${r.account ?? ""}-${r.scope ?? ""}-recent`} className="bg-background border border-border rounded-md px-3 py-2 text-xs">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="font-medium text-text-primary capitalize">{formatRateLimitSummaryLabel(r)}</div>
                      <div className="text-text-muted mt-0.5">
                        {formatNumber(r.count)} hits · last {relativeTime(r.last_seen_at)} · cooldown {formatCooldown(r.cooldown_seconds)}
                      </div>
                    </div>
                    <StatusBadge status={r.active_now ? "warning" : "idle"} label={r.active_now ? "cooling" : "seen"} />
                  </div>
                  {r.reason && <div className="mt-1 text-text-muted truncate">{r.reason}</div>}
                </div>
              ))}
            </div>
          </div>
        )}
        <DataTable data={(rateLimits?.events ?? []).slice(0, 40)} columns={rateLimitColumns} />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4 mb-6">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-3">Backfill Phase Guide</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2 text-xs">
          <div className="bg-background border border-border rounded-md p-3"><b className="text-text-primary">realtime</b><p className="text-text-muted mt-1">new rows are mostly current-hour messages</p></div>
          <div className="bg-background border border-border rounded-md p-3"><b className="text-text-primary">draining</b><p className="text-text-muted mt-1">collector is still ingesting older history</p></div>
          <div className="bg-background border border-border rounded-md p-3"><b className="text-text-primary">current</b><p className="text-text-muted mt-1">cyclic source is fresh, not a deep backlog</p></div>
          <div className="bg-background border border-border rounded-md p-3"><b className="text-text-primary">crawl</b><p className="text-text-muted mt-1">open-ended discovery queue, expected not to hit zero</p></div>
        </div>
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
