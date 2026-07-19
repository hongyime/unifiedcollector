import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { FilterDropdown } from "../../components/ui/FilterDropdown";
import { MetricCard } from "../../components/ui/MetricCard";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";

const sourceOptions = [
  { value: "whatsapp", label: "WhatsApp co-message" },
  { value: "instagram", label: "Instagram follows" },
  { value: "strava", label: "Strava activity edges" },
  { value: "github", label: "GitHub repo edges" },
  { value: "x", label: "X follow edges" },
];

export function GraphPage() {
  const [source, setSource] = useState("whatsapp");
  const [limit, setLimit] = useState("5000");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["graph", source, limit],
    queryFn: () => api.graph(source, parseInt(limit, 10)),
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="Raw Relationship Edges" subtitle="Collector pre-resolution edges" onRefresh={() => refetch()} />
      <div className="flex items-center gap-3 mb-4">
        <FilterDropdown label="Edge Source" value={source} onChange={setSource} options={sourceOptions} />
        <FilterDropdown label="Edge Limit" value={limit} onChange={setLimit} options={[
          { value: "1000", label: "1,000" },
          { value: "5000", label: "5,000" },
          { value: "10000", label: "10,000" },
          { value: "50000", label: "50,000" },
        ]} />
      </div>
      {isLoading ? <LoadingSpinner /> : data && (
        <>
          <div className="grid grid-cols-2 gap-4 mb-6">
            <MetricCard label="Nodes" value={data.nodes.length} status="info" />
            <MetricCard label="Edges" value={data.edges.length} status="info" />
          </div>
          <div className="bg-surface rounded-lg border border-border p-4">
            <table className="w-full text-sm">
              <thead><tr className="text-left text-text-muted border-b border-border">
                <th className="pb-2">Source User</th><th className="pb-2">Target User</th><th className="pb-2">Edge Type</th>
              </tr></thead>
              <tbody>
                {data.edges.map((e, i) => (
                  <tr key={i} className="border-b border-border/50 hover:bg-white/5">
                    <td className="py-2 font-medium">{e.source_user}</td>
                    <td className="py-2 font-medium">{e.target_user}</td>
                    <td className="py-2 text-xs text-text-muted">{e.edge_type}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
