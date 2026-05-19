import { useQuery } from "@tanstack/react-query";
import { Header } from "../../components/layout/Header";
import { MetricCard } from "../../components/ui/MetricCard";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { JSONViewer } from "../../components/shared/JSONViewer";
import { api } from "../../services/api";
import { Database, HardDrive, Server } from "lucide-react";

export function HealthPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 5_000,
  });

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  const overall = data?.status === "ok" ? "online" : "error";

  return (
    <div>
      <Header title="Health" subtitle="System health status" onRefresh={() => refetch()} />

      <div className="grid grid-cols-3 gap-3 mb-6">
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
          label="Drive"
          value={data?.drive ?? "unknown"}
          status={data?.drive === "mounted" ? "success" : "error"}
          icon={<HardDrive className="w-5 h-5" />}
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
