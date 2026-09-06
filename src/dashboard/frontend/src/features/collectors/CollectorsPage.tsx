import { Header } from "../../components/layout/Header";
import { DataTable } from "../../components/ui/DataTable";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { useCollectors } from "../../hooks/useCollectors";
import { relativeTime } from "../../utils/formatters";
import type { ColumnDef } from "@tanstack/react-table";
import type { CollectorStatus } from "../../services/types";

function mapStatus(s: string): "online" | "processing" | "idle" | "error" | "offline" {
  if (s === "running") return "processing";
  if (s === "idle" || s === "pending") return "idle";
  if (s === "completed") return "online";
  return "offline";
}

const columns: ColumnDef<CollectorStatus, unknown>[] = [
  {
    accessorKey: "service",
    header: "Collector",
    cell: (info) => <span className="uppercase font-medium text-text-primary">{info.getValue() as string}</span>,
  },
  {
    accessorKey: "status",
    header: "Status",
    cell: (info) => {
      const s = info.getValue() as string;
      return <StatusBadge status={mapStatus(s)} label={s} />;
    },
  },
  {
    accessorKey: "last_processed_id",
    header: "Last Processed",
    cell: (info) => (info.getValue() as string) ?? "-",
  },
  {
    accessorKey: "last_processed_at",
    header: "Last Active",
    cell: (info) => relativeTime(info.getValue() as string | null),
  },
];

export function CollectorsPage() {
  const { data, isLoading, error, refetch } = useCollectors();

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Collectors" subtitle="Service cursor status" onRefresh={() => refetch()} />
      <div className="bg-surface rounded-lg border border-border p-4">
        <DataTable data={data ?? []} columns={columns} />
      </div>
    </div>
  );
}
