import { useQuery } from "@tanstack/react-query";
import { Header } from "../../components/layout/Header";
import { MetricCard } from "../../components/ui/MetricCard";
import { DataTable } from "../../components/ui/DataTable";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { api } from "../../services/api";
import { formatBytes, formatDuration, formatNumber, relativeTime } from "../../utils/formatters";
import { Archive, Database, HardDrive, Activity, AlertCircle, Clock3, ShieldAlert, Puzzle } from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type {
  HourlyIngestionRow,
  MediaStats,
  MessagingCoverageRow,
  RateLimitEvent,
  RateLimitRecentSummary,
  BrowserExtensionIssue,
  SourceCollectionMatrixRow,
} from "../../services/types";

function liveBadgeStatus(status: MediaStats["live"]) {
  if (status === "live") return "online";
  if (status === "stale" || status === "degraded") return "warning";
  if (status === "dead" || status === "unpaired" || status === "unreachable") return "error";
  return "idle";
}

function formatVersion(version: string | null | undefined) {
  if (!version) return "unknown";
  return version.toLowerCase().startsWith("v") ? version : `v${version}`;
}

function extensionIssueTitle(issue: BrowserExtensionIssue) {
  const endpoint = issue.endpoint ? ` · ${issue.endpoint.replace(/_/g, " ")}` : "";
  return `${issue.platform}${endpoint}`;
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
          <div>
            <div className="uppercase font-medium text-text-primary">{info.getValue() as string}</div>
            {row.collection_mode && (
              <div className="text-[10px] uppercase tracking-wide text-text-muted">
                {row.collection_mode}
              </div>
            )}
          </div>
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
          {row.live && row.live !== "live" && row.health_detail && (
            <div className="text-[10px] text-warning max-w-[320px] truncate">
              {row.health_detail}
            </div>
          )}
          {(row.stats_stale || row.stats_error) && (
            <div className="text-[10px] text-warning max-w-[320px] truncate">
              media totals {row.stats_stale ? "cached" : "degraded"}
              {row.stats_error ? ` (${row.stats_error})` : ""}
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

function blockerBadgeStatus(severity: string) {
  if (severity === "ok") return "online";
  if (severity === "error") return "error";
  return "warning";
}

function renderWindow(row: SourceCollectionMatrixRow["current_hour"]) {
  return (
    <div>
      <div>{formatNumber(row.records)} rows · {formatNumber(row.media_items)} files</div>
      <div className="text-[10px] uppercase tracking-wide text-text-muted">
        {formatNumber(row.messages)} msgs · {formatNumber(row.rate_limits)} 429 · {formatNumber(row.access_errors)} auth/other
      </div>
    </div>
  );
}

const sourceMatrixColumns: ColumnDef<SourceCollectionMatrixRow, unknown>[] = [
  {
    accessorKey: "source",
    header: "Source",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div className="flex items-center gap-2">
          <StatusBadge status={liveBadgeStatus(row.status)} label={row.status} />
          <div>
            <div className="uppercase font-medium text-text-primary">{info.getValue() as string}</div>
            <div className="text-[10px] uppercase tracking-wide text-text-muted">
              {row.collection_methods.length ? row.collection_methods.join(" + ") : row.collection_mode ?? "unknown mode"}
            </div>
          </div>
        </div>
      );
    },
  },
  {
    accessorKey: "age_seconds",
    header: "Freshness",
    cell: (info) => {
      const row = info.row.original;
      const value = info.getValue() as number | null;
      return (
        <div>
          <div>{value == null ? "never" : `${formatDuration(value)} ago`}</div>
          <div className="text-[10px] uppercase tracking-wide text-text-muted truncate max-w-[260px]">
            {row.freshness_basis ?? "-"}
          </div>
          {row.source_health_status && (
            <div className="text-[10px] uppercase tracking-wide text-text-muted truncate max-w-[260px]">
              worker {row.source_health_status}
              {row.source_health_last_success_at
                ? ` · progress ${relativeTime(row.source_health_last_success_at)}`
                : row.source_health_updated_at
                  ? ` · updated ${relativeTime(row.source_health_updated_at)}`
                  : ""}
            </div>
          )}
        </div>
      );
    },
  },
  {
    accessorKey: "current_hour",
    header: "This Hour",
    cell: (info) => renderWindow(info.getValue() as SourceCollectionMatrixRow["current_hour"]),
  },
  {
    accessorKey: "last_24h",
    header: "24h",
    cell: (info) => renderWindow(info.getValue() as SourceCollectionMatrixRow["last_24h"]),
  },
  {
    accessorKey: "total_media_items",
    header: "Media Vault",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div>
          <div>{formatNumber(info.getValue() as number)} files</div>
          <div className="text-[10px] uppercase tracking-wide text-text-muted">
            {formatBytes(row.total_media_bytes)} · {relativeTime(row.latest_media_at)}
          </div>
        </div>
      );
    },
  },
  {
    accessorKey: "blocker",
    header: "Blocker / Action",
    cell: (info) => {
      const blocker = info.getValue() as SourceCollectionMatrixRow["blocker"];
      return (
        <div>
          <div className="flex items-center gap-2">
            <StatusBadge status={blockerBadgeStatus(blocker.severity)} label={blocker.kind.replace(/_/g, " ")} />
            <span className={blocker.severity === "ok" ? "text-text-muted" : "text-warning"}>{blocker.summary}</span>
          </div>
          <div className="text-[10px] text-text-muted mt-1">{blocker.next_action}</div>
        </div>
      );
    },
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
          {row.coverage_note ? (
            <div className="mt-1 max-w-[260px] text-[10px] leading-snug text-text-muted normal-case tracking-normal">
              {row.coverage_note}
            </div>
          ) : null}
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
          {row.native_people_basis ? (
            <div className="text-[10px] text-text-muted">{row.native_people_basis}</div>
          ) : null}
          {row.native_bots ? (
            <div className="text-[10px] text-text-muted">{formatNumber(row.native_bots)} bots excluded</div>
          ) : null}
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
          {row.beeper_people_basis ? (
            <div className="text-[10px] text-text-muted">{row.beeper_people_basis}</div>
          ) : null}
          {row.beeper_message_senders && row.beeper_people_basis !== "distinct beeper message senders" ? (
            <div className="text-[10px] text-text-muted">
              {formatNumber(row.beeper_message_senders)} message senders
            </div>
          ) : null}
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
    header: "Rate-limit Events",
    cell: (info) => {
      const value = info.getValue() as number;
      return value > 0 ? <span className="text-warning font-semibold">{value}</span> : <span className="text-text-muted">0</span>;
    },
  },
  {
    accessorKey: "access_errors",
    header: "Auth/Other",
    cell: (info) => {
      const value = info.getValue() as number;
      return value > 0 ? <span className="text-danger font-semibold">{value}</span> : <span className="text-text-muted">0</span>;
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

function isInstrumentedRateLimitStatus(statusCode: number | null) {
  return statusCode === 429 || statusCode === null;
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
    accessorKey: "status_code",
    header: "HTTP",
    cell: (info) => {
      const value = info.getValue() as number | null;
      if (value === 429) return <span className="text-warning font-semibold">429</span>;
      return value ? <span className="text-danger font-semibold">{value}</span> : <span className="text-text-muted">—</span>;
    },
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

  const { data: sourceMatrix } = useQuery({
    queryKey: ["source-matrix"],
    queryFn: api.sourceMatrix,
    refetchInterval: 30_000,
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
  const degradedCollectors = collectorsLive?.degraded ?? Math.max(0, totalCollectors - liveCollectors);
  const newestHour = hourly?.[0]?.hour;
  const currentHourRows = hourly?.filter((r) => r.hour === newestHour) ?? [];
  const currentSummary = sourceMatrix?.summary?.current_hour;
  const previousSummary = sourceMatrix?.summary?.last_complete_hour;
  const currentRows = currentSummary?.records ?? currentHourRows.reduce((s, r) => s + r.records, 0);
  const currentMessages = currentSummary?.messages ?? currentHourRows.reduce((s, r) => s + r.messages, 0);
  const currentFiles = currentSummary?.media_items ?? currentHourRows.reduce((s, r) => s + r.media_items, 0);
  const currentRateLimitEvents = currentSummary?.rate_limits ?? currentHourRows.reduce((s, r) => s + r.rate_limits, 0);
  const currentAccessErrors = currentSummary?.access_errors ?? currentHourRows.reduce((s, r) => s + (r.access_errors ?? 0), 0);
  const currentActiveSources = currentSummary?.active_sources ?? new Set(currentHourRows.map((r) => r.source)).size;
  const nowMs = Date.now();
  const activeCursorLimits = (rateLimits?.active ?? []).filter((r) => {
    const expiryMs = r.active_until ? new Date(r.active_until).getTime() : Number.NaN;
    return r.status === "blocked" && (!r.active_until || Number.isNaN(expiryMs) || expiryMs > nowMs);
  });
  const expiredCursorLimits = (rateLimits?.cursor_history ?? []).filter((r) => {
    const expiryMs = r.active_until ? new Date(r.active_until).getTime() : Number.NaN;
    return r.status === "blocked" && r.active_until && !Number.isNaN(expiryMs) && expiryMs <= nowMs;
  });
  const activeCursorSources = new Set(activeCursorLimits.map((r) => formatRateLimitService(r.service)));
  const recentLimitSummaries = rateLimits?.recent_summary ?? [];
  const activeEventLimits = recentLimitSummaries.filter(
    (r) => r.active_now && !activeCursorSources.has(normalizeRateLimitSource(r.source)),
  );
  const recentRecordedRateLimitEvents = (rateLimits?.events ?? [])
    .filter((r) => isInstrumentedRateLimitStatus(r.status_code)).length;
  const recentAccessEvents = (rateLimits?.events ?? [])
    .filter((r) => !isInstrumentedRateLimitStatus(r.status_code)).length;
  const recentRateLimitScopes = recentLimitSummaries.length;
  const activeRateLimits = activeCursorLimits.length + activeEventLimits.length;
  const vault = health?.vault;
  const vaultOk = vault?.available && vault?.writable;
  const vaultSidecarDlqRows = vault?.artifacts_queued ?? 0;
  const vaultFailedMetadataRows = vault?.artifacts_partial ?? 0;
  const vaultMissingSidecarRows = vault?.artifacts_missing_sidecar ?? 0;
  const vaultMissingPrefix = vault?.artifacts_missing_sidecar_estimated ? "~" : "";
  const vaultIssues = vaultSidecarDlqRows + vaultFailedMetadataRows;
  const vaultWarnings = vaultIssues + vaultMissingSidecarRows;
  const backups = health?.backups;
  const extension = health?.browser_extension;
  const extensionIssues = extension?.issues ?? [];
  const extensionHooks = extension?.hooks ?? [];
  const extensionIngest = extension?.ingest ?? [];
  const backupStatus = backups?.status ?? "missing";
  const backupValue =
    backupStatus === "ok" ? "Fresh" :
    backupStatus === "refreshing" ? "Refreshing" :
    backupStatus === "stale" ? "Stale" :
    backupStatus === "error" ? "Error" :
    "Missing";
  const backupSublabel = backups?.latest_age_seconds != null
    ? `${formatDuration(backups.latest_age_seconds)} old · ${formatBytes(backups.latest_size_bytes)} · ${formatNumber(backups.backup_count)} kept`
    : backups?.in_progress
      ? "backup running"
      : "no restorable dump found";
  const backupDetail = backups?.stale_in_progress_count
    ? `${backupSublabel} · ${formatNumber(backups.stale_in_progress_count)} stale temp`
    : backupSublabel;

  return (
    <div>
      <Header title="Dashboard" subtitle="System overview" onRefresh={() => refetch()} />

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3 mb-6">
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
          sublabel={liveCollectors === totalCollectors ? "all live" : `${degradedCollectors} need attention`}
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
              ? `${formatBytes(vault.free_bytes)} free · ${formatNumber(vaultSidecarDlqRows)} DLQ · ${formatNumber(vaultFailedMetadataRows)} partial · ${vaultMissingPrefix}${formatNumber(vaultMissingSidecarRows)} historical missing`
              : health?.drive ?? "unknown"
          }
          status={vaultOk && vaultWarnings === 0 ? "success" : vaultOk ? "warning" : "error"}
        />
        <MetricCard
          label="DB Backups"
          value={backupValue}
          sublabel={backupDetail}
          status={backupStatus === "ok" ? "success" : backupStatus === "refreshing" || backupStatus === "stale" ? "warning" : "error"}
          icon={<Archive className="w-5 h-5" />}
        />
        <MetricCard
          label="This Hour"
          value={formatNumber(currentRows)}
          sublabel={`${formatNumber(currentActiveSources)} active sources · ${formatNumber(currentMessages)} msgs · ${formatNumber(currentFiles)} files · ${formatNumber(currentRateLimitEvents)} 429 · ${formatNumber(currentAccessErrors)} auth/other`}
          status={currentRateLimitEvents || currentAccessErrors ? "warning" : currentRows > 0 || currentFiles > 0 ? "success" : "warning"}
          icon={<Clock3 className="w-5 h-5" />}
        />
        <MetricCard
          label="Previous Hour"
          value={formatNumber(previousSummary?.records ?? 0)}
          sublabel={`${formatNumber(previousSummary?.messages ?? 0)} msgs · ${formatNumber(previousSummary?.media_items ?? 0)} files · ${formatNumber(previousSummary?.active_sources ?? 0)} active sources`}
          status={(previousSummary?.rate_limits ?? 0) || (previousSummary?.access_errors ?? 0) ? "warning" : "success"}
          icon={<Clock3 className="w-5 h-5" />}
        />
        <MetricCard
          label="Rate Limits"
          value={activeRateLimits ? `${activeRateLimits} active` : `${recentRateLimitScopes}`}
          sublabel={
            activeRateLimits
              ? `${formatNumber(recentRecordedRateLimitEvents)} instrumented rate-limit events · ${formatNumber(recentAccessEvents)} auth/other last 24h`
              : "recorded source/account scopes last 24h"
          }
          status={activeRateLimits || recentRecordedRateLimitEvents || recentAccessEvents ? "warning" : "success"}
          icon={<ShieldAlert className="w-5 h-5" />}
        />
        <MetricCard
          label="Chrome Extension"
          value={
            !extension
              ? "Unknown"
              : extensionIssues.length
                ? `${formatNumber(extensionIssues.length)} issues`
                : "Current"
          }
          sublabel={
            extension?.expected_version
              ? `expected ${formatVersion(extension.expected_version)}`
              : "expected version unknown"
          }
          status={!extension ? "idle" : extensionIssues.length ? "warning" : "success"}
          icon={<Puzzle className="w-5 h-5" />}
        />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4 mb-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-xs uppercase tracking-wider text-text-muted">Hourly Ingestion</h2>
            <p className="text-xs text-text-muted mt-1">Real rows/files by hour. Rate-limit events are recorded collector signals; Auth/Other is non-429 session, access, or quota trouble.</p>
          </div>
        </div>
        <DataTable data={(hourly ?? []).slice(0, 80)} columns={hourlyColumns} />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4 mb-6">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-4">HTTP Pressure And Session Events</h2>
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
                  {r.active_until ? `recorded cooldown until ${formatClock(r.active_until)}` : "recorded cooldown"}
                  {` · ${formatNumber(r.count)} events`}
                </div>
              </div>
            ))}
          </div>
        )}
        {activeCursorLimits.length === 0 && activeEventLimits.length === 0 && recentLimitSummaries.length > 0 && (
          <div className="mb-3 bg-background border border-border rounded-md px-3 py-2 text-xs text-text-muted">
            No active cooldowns right now. The scopes below are recent historical HTTP pressure or session events
            {expiredCursorLimits.length ? `; ${formatNumber(expiredCursorLimits.length)} expired persisted cooldown cursor${expiredCursorLimits.length === 1 ? "" : "s"} kept for audit.` : "."}
          </div>
        )}
        {recentLimitSummaries.length > 0 && (
          <div className="mb-4">
            <div className="text-[10px] uppercase tracking-wide text-text-muted mb-2">Recorded Rate-Limit/Auth Scopes</div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
              {recentLimitSummaries.slice(0, 8).map((r) => (
                <div key={`${r.source}-${r.account ?? ""}-${r.scope ?? ""}-recent`} className="bg-background border border-border rounded-md px-3 py-2 text-xs">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="font-medium text-text-primary capitalize">{formatRateLimitSummaryLabel(r)}</div>
                      <div className="text-text-muted mt-0.5">
                        HTTP {r.status_code ?? "—"} · {formatNumber(r.count)} events · last {relativeTime(r.last_seen_at)} · recorded cooldown {formatCooldown(r.cooldown_seconds)}
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

      {extension && (
        <div className="bg-surface rounded-lg border border-border p-4 mb-6">
          <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between mb-4">
            <div>
              <h2 className="text-xs uppercase tracking-wider text-text-muted">Chrome Extension</h2>
              <p className="text-xs text-text-muted mt-1">
                Expected {formatVersion(extension.expected_version)}. Active hooks and browser-ingest events are compared against that build.
              </p>
            </div>
            <StatusBadge
              status={extensionIssues.length ? "warning" : "online"}
              label={extensionIssues.length ? `${formatNumber(extensionIssues.length)} needs reload` : "current"}
            />
          </div>

          {extensionIssues.length > 0 && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-2 mb-4">
              {extensionIssues.slice(0, 8).map((issue, index) => (
                <div
                  key={`${issue.platform}-${issue.endpoint ?? ""}-${issue.kind}-${index}`}
                  className="bg-warning/10 border border-warning/30 rounded-md px-3 py-2 text-xs"
                >
                  <div className="font-medium text-warning capitalize">{extensionIssueTitle(issue)}</div>
                  <div className="text-text-muted mt-0.5">{issue.detail}</div>
                  {(issue.extension_version || issue.expected_version || issue.age_seconds != null) && (
                    <div className="text-text-muted mt-1">
                      {issue.extension_version && `loaded ${formatVersion(issue.extension_version)}`}
                      {issue.extension_version && issue.expected_version && " · "}
                      {issue.expected_version && `expected ${formatVersion(issue.expected_version)}`}
                      {issue.age_seconds != null && ` · last heartbeat ${formatDuration(issue.age_seconds)} ago`}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            <div>
              <div className="text-[10px] uppercase tracking-wide text-text-muted mb-2">Hooks</div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {extensionHooks.slice(0, 6).map((hook) => (
                  <div key={hook.platform} className="bg-background border border-border rounded-md px-3 py-2 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium text-text-primary capitalize">{hook.platform}</span>
                      <StatusBadge status={hook.version_ok ? "online" : "warning"} label={formatVersion(hook.extension_version)} />
                    </div>
                    <div className="text-text-muted mt-1">
                      heartbeat {formatDuration(hook.age_seconds)} ago · {formatNumber(hook.owner_count)} owners
                    </div>
                    <div className="text-text-muted">
                      {formatNumber(hook.probes_sent)} probes · {formatNumber(hook.samples_shipped)} samples
                    </div>
                  </div>
                ))}
                {extensionHooks.length === 0 && (
                  <div className="text-xs text-text-muted">No browser hook heartbeat rows found.</div>
                )}
              </div>
            </div>

            <div>
              <div className="text-[10px] uppercase tracking-wide text-text-muted mb-2">Recent Browser Ingest</div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {extensionIngest.slice(0, 8).map((row) => (
                  <div
                    key={`${row.platform}-${row.endpoint}`}
                    className="bg-background border border-border rounded-md px-3 py-2 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium text-text-primary capitalize">
                        {row.platform} · {row.endpoint.replace(/_/g, " ")}
                      </span>
                      <StatusBadge status={row.version_ok ? "online" : "warning"} label={formatVersion(row.extension_version)} />
                    </div>
                    <div className="text-text-muted mt-1">
                      {formatNumber(row.requests)} POSTs · saw {formatNumber(row.observed_count)} · stored {formatNumber(row.stored_count)}
                    </div>
                    <div className="text-text-muted">last seen {formatDuration(row.age_seconds)} ago</div>
                  </div>
                ))}
                {extensionIngest.length === 0 && (
                  <div className="text-xs text-text-muted">No browser ingest events in the last 24h.</div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="bg-surface rounded-lg border border-border p-4">
        <div className="flex items-center justify-between gap-3 mb-4">
          <div>
            <h2 className="text-xs uppercase tracking-wider text-text-muted">Source Collection Matrix</h2>
            <p className="text-xs text-text-muted mt-1">
              Current hour started {formatClock(sourceMatrix?.current_hour_started_at)}; previous hour is the last full 60-minute window.
            </p>
          </div>
        </div>
        <DataTable data={sourceMatrix?.sources ?? []} columns={sourceMatrixColumns} />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4 mt-6">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-4">Collection Stats by Source</h2>
        {sLoading ? <LoadingSpinner /> : <DataTable data={stats ?? []} columns={columns} />}
      </div>

      <div className="bg-surface rounded-lg border border-border p-4 mt-6">
        <div className="mb-4">
          <h2 className="text-xs uppercase tracking-wider text-text-muted">Messaging Coverage</h2>
          <p className="mt-1 text-xs text-text-muted">
            Telegram and WhatsApp native collectors are canonical. Beeper remains a mirror/backstop for those networks and is canonical for Discord, Slack, LinkedIn, Signal, Instagram DMs, and Beeper-only rooms.
          </p>
        </div>
        <DataTable data={messagingCoverage ?? []} columns={messagingColumns} />
      </div>
    </div>
  );
}
