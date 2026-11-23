import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Header } from "../../components/layout/Header";
import { DataTable } from "../../components/ui/DataTable";
import { FilterDropdown } from "../../components/ui/FilterDropdown";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { EmptyState } from "../../components/ui/EmptyState";
import { StatusBadge } from "../../components/ui/StatusBadge";
import { Button } from "../../components/ui/Button";
import { api } from "../../services/api";
import { relativeTime } from "../../utils/formatters";
import { SOURCES } from "../../utils/constants";
import { AlertTriangle, Play, Archive, RotateCw } from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import type { DLQItem } from "../../services/types";

function dlqStatusToBadge(status: string | null | undefined): { status: "warning" | "processing" | "error" | "idle"; label: string } {
  switch (status) {
    case "pending":
      return { status: "warning", label: "Pending" };
    case "in_progress":
      return { status: "processing", label: "In Progress" };
    case "failed":
      return { status: "error", label: "Failed" };
    case "archived":
      return { status: "idle", label: "Archived" };
    default:
      return { status: "idle", label: status ?? "Unknown" };
  }
}

const sourceOptions = [
  { value: "", label: "All sources" },
  ...SOURCES.map((s) => ({ value: s, label: s.charAt(0).toUpperCase() + s.slice(1) })),
];

const statusOptions = [
  { value: "", label: "All statuses" },
  { value: "pending", label: "Pending" },
  { value: "in_progress", label: "In Progress" },
  { value: "failed", label: "Failed" },
  { value: "archived", label: "Archived" },
];

export function DLQPage() {
  const qc = useQueryClient();
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["dlq", source, status],
    queryFn: () => api.dlq(source || undefined, status || undefined),
    refetchInterval: 15_000,
  });

  const retryMutation = useMutation({
    mutationFn: (id: number) => api.retryDLQ(id),
    onSuccess: (res) => {
      setActionMsg(`Item #${res.id} queued for retry`);
      qc.invalidateQueries({ queryKey: ["dlq"] });
    },
  });

  const redriveMutation = useMutation({
    mutationFn: () => api.redrivePendingDLQ(source || undefined),
    onSuccess: (res) => {
      setActionMsg(`Redrove ${res.redriven} pending item(s)`);
      qc.invalidateQueries({ queryKey: ["dlq"] });
    },
  });

  const archiveMutation = useMutation({
    mutationFn: () => api.archiveFailedDLQ(source || undefined, false),
    onSuccess: (res) => {
      setActionMsg(`Archived ${res.count} failed item(s)`);
      qc.invalidateQueries({ queryKey: ["dlq"] });
    },
  });

  const columns = useMemo<ColumnDef<DLQItem, unknown>[]>(
    () => [
      { accessorKey: "id", header: "ID" },
      {
        accessorKey: "source",
        header: "Source",
        cell: (info) => <span className="uppercase text-xs">{info.getValue() as string}</span>,
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: (info) => {
          const cfg = dlqStatusToBadge(info.getValue() as string | null | undefined);
          return <StatusBadge status={cfg.status} label={cfg.label} />;
        },
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
        accessorKey: "next_retry_at",
        header: "Next Retry",
        cell: (info) => {
          const v = info.getValue() as string | null | undefined;
          return v ? relativeTime(v) : "-";
        },
      },
      {
        accessorKey: "created_at",
        header: "Created",
        cell: (info) => relativeTime(info.getValue() as string),
      },
      {
        id: "actions",
        header: "Action",
        cell: ({ row }) => {
          const item = row.original;
          return (
            <Button
              size="sm"
              variant="ghost"
              loading={retryMutation.isPending && retryMutation.variables === item.id}
              onClick={() => retryMutation.mutate(item.id)}
              icon={<RotateCw className="w-3 h-3" />}
              title="Reset next_retry_at and retry now"
            >
              Retry
            </Button>
          );
        },
      },
    ],
    [retryMutation],
  );

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Dead Letter Queue" subtitle="Failed collection items" onRefresh={() => refetch()} />
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div className="flex items-center gap-3">
          <FilterDropdown label="Source" value={source} onChange={setSource} options={sourceOptions} />
          <FilterDropdown label="Status" value={status} onChange={setStatus} options={statusOptions} />
          {actionMsg && (
            <span className="text-xs text-text-muted bg-surface px-2.5 py-1 rounded border border-border">
              {actionMsg}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            loading={archiveMutation.isPending}
            onClick={() => archiveMutation.mutate()}
            icon={<Archive className="w-3.5 h-3.5" />}
            title="Archive all failed dead letters (mark status='archived')"
          >
            Archive Failed
          </Button>
          <Button
            size="sm"
            variant="primary"
            loading={redriveMutation.isPending}
            onClick={() => redriveMutation.mutate()}
            icon={<Play className="w-3.5 h-3.5" />}
            title="Immediately redrive pending dead letters"
          >
            Re-drive Pending
          </Button>
        </div>
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
