import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Header } from "../../components/layout/Header";
import { DataTable } from "../../components/ui/DataTable";
import { FilterDropdown } from "../../components/ui/FilterDropdown";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { EmptyState } from "../../components/ui/EmptyState";
import { api } from "../../services/api";
import { relativeTime } from "../../utils/formatters";
import { SOURCES } from "../../utils/constants";
import { AlertTriangle } from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type { DLQItem } from "../../services/types";

const columns: ColumnDef<DLQItem, unknown>[] = [
  { accessorKey: "id", header: "ID" },
  {
    accessorKey: "source",
    header: "Source",
    cell: (info) => <span className="uppercase text-xs">{info.getValue() as string}</span>,
  },
  { accessorKey: "entity_id", header: "Entity", cell: (info) => (info.getValue() as string) ?? "-" },
  { accessorKey: "content_id", header: "Content", cell: (info) => (info.getValue() as string) ?? "-" },
  {
    accessorKey: "error_message",
    header: "Error",
    cell: (info) => {
      const v = (info.getValue() as string) ?? "";
      return <span className="text-error" title={v}>{v.length > 60 ? v.slice(0, 57) + "..." : v}</span>;
    },
  },
  { accessorKey: "retry_count", header: "Retries" },
  {
    accessorKey: "created_at",
    header: "Created",
    cell: (info) => relativeTime(info.getValue() as string),
  },
];

const sourceOptions = [
  { value: "", label: "All sources" },
  ...SOURCES.map((s) => ({ value: s, label: s.charAt(0).toUpperCase() + s.slice(1) })),
];

export function DLQPage() {
  const [source, setSource] = useState("");
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["dlq", source],
    queryFn: () => api.dlq(source || undefined),
    refetchInterval: 15_000,
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Dead Letter Queue" subtitle="Failed collection items" onRefresh={() => refetch()} />
      <div className="flex items-center gap-3 mb-4">
        <FilterDropdown label="Source" value={source} onChange={setSource} options={sourceOptions} />
      </div>
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? (
          <LoadingSpinner />
        ) : !data?.length ? (
          <EmptyState icon={<AlertTriangle className="w-10 h-10" />} title="No dead letters" description="All items processed successfully." />
        ) : (
          <DataTable data={data} columns={columns} />
        )}
      </div>
    </div>
  );
}
