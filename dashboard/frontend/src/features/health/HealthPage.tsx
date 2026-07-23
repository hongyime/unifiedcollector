import { useQuery } from "@tanstack/react-query";
import { Header } from "../../components/layout/Header";
import { MetricCard } from "../../components/ui/MetricCard";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { JSONViewer } from "../../components/shared/JSONViewer";
import { api } from "../../services/api";
import { formatBytes, formatDuration, formatNumber } from "../../utils/formatters";
import { Archive, Database, HardDrive, Server } from "lucide-react";

export function HealthPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 5_000,
  });

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  const overall = data?.status === "ok" ? "online" : "error";
  const vault = data?.vault;
  const vaultOk = vault?.available && vault?.writable;
  const vaultIssues = (vault?.artifacts_queued ?? 0) + (vault?.artifacts_partial ?? 0);
  const backups = data?.backups;
  const backupStatus = backups?.status ?? "missing";
  const backupOk = backupStatus === "ok";
  const backupValue =
    backupStatus === "ok" ? "Fresh" :
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
      <Header title="Health" subtitle="System health status" onRefresh={() => refetch()} />

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3 mb-6">
        <MetricCard
          label="Overall"
          value={data?.status === "ok" ? "Healthy" : "Degraded"}
          status={data?.status === "ok" ? "success" : "error"}
          icon={<Server className="w-5 h-5" />}
        />
        <MetricCard
          label="Database"
          value={data?.database ?? "unknown"}
          status={data?.database === "healthy" ? "success" : "error"}
          icon={<Database className="w-5 h-5" />}
        />
        <MetricCard
          label="Vault"
          value={vaultOk ? "Writable" : "Blocked"}
          sublabel={vault?.free_bytes != null ? `${formatBytes(vault.free_bytes)} free` : data?.drive ?? "unknown"}
          status={vaultOk ? "success" : "error"}
          icon={<HardDrive className="w-5 h-5" />}
        />
        <MetricCard
          label="Artifact Health"
          value={vaultIssues ? formatNumber(vaultIssues) : "OK"}
          sublabel={
            vault
              ? `${formatNumber(vault.artifacts_queued)} queued · ${formatNumber(vault.artifacts_partial)} partial`
              : "unknown"
          }
          status={vaultIssues ? "warning" : "success"}
        />
        <MetricCard
          label="DB Backups"
          value={backupValue}
          sublabel={backupDetail}
          status={backupOk ? "success" : backupStatus === "stale" ? "warning" : "error"}
          icon={<Archive className="w-5 h-5" />}
        />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-xs uppercase tracking-wider text-text-muted">Status Details</h2>
          <StatusBadge status={overall} />
        </div>
        <JSONViewer data={data} />
      </div>
    </div>
  );
}
