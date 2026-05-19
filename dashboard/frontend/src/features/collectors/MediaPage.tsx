import { useState } from "react";
import { Header } from "../../components/layout/Header";
import { DataTable } from "../../components/ui/DataTable";
import { SearchBar } from "../../components/ui/SearchBar";
import { FilterDropdown } from "../../components/ui/FilterDropdown";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { useMedia } from "../../hooks/useMedia";
import { formatBytes, relativeTime } from "../../utils/formatters";
import { SOURCES } from "../../utils/constants";
import type { ColumnDef } from "@tanstack/react-table";
import type { MediaItem } from "../../services/types";

const columns: ColumnDef<MediaItem, unknown>[] = [
  {
    accessorKey: "source",
    header: "Source",
    cell: (info) => <span className="uppercase text-xs">{info.getValue() as string}</span>,
  },
  { accessorKey: "entity_name", header: "Entity" },
  { accessorKey: "content_type", header: "Type" },
  {
    accessorKey: "filename",
    header: "Filename",
    cell: (info) => {
      const v = info.getValue() as string;
      return <span title={v}>{v.length > 40 ? v.slice(0, 37) + "..." : v}</span>;
    },
  },
  {
    accessorKey: "file_size",
    header: "Size",
    cell: (info) => formatBytes(info.getValue() as number | null),
  },
  {
    accessorKey: "collected_at",
    header: "Collected",
    cell: (info) => relativeTime(info.getValue() as string),
  },
];

const sourceOptions = [
  { value: "", label: "All sources" },
  ...SOURCES.map((s) => ({ value: s, label: s.charAt(0).toUpperCase() + s.slice(1) })),
];

export function MediaPage() {
  const [source, setSource] = useState("");
  const { data, isLoading, error, refetch } = useMedia(source || undefined, 100);

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Media" subtitle="Collected media items" onRefresh={() => refetch()} />

      <div className="flex items-center gap-3 mb-4">
        <FilterDropdown label="Source" value={source} onChange={setSource} options={sourceOptions} />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : <DataTable data={data ?? []} columns={columns} />}
      </div>
    </div>
  );
}
