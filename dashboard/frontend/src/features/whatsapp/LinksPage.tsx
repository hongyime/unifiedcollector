import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { FilterDropdown } from "../../components/ui/FilterDropdown";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { formatTimestamp } from "../../utils/formatters";

const typeOptions = [
  { value: "", label: "All types" },
  { value: "url", label: "URL" },
  { value: "group_invite", label: "Group invite" },
  { value: "group_invite_restricted", label: "Restricted invite" },
  { value: "contact_link", label: "Contact link" },
];

const statusOptions = [
  { value: "", label: "All statuses" },
  { value: "pending", label: "Pending" },
  { value: "fetched", label: "Fetched" },
  { value: "error", label: "Error" },
];

const typeBadge: Record<string, string> = {
  url: "bg-info/20 text-info",
  group_invite: "bg-warning/20 text-warning",
  group_invite_restricted: "bg-danger/20 text-danger",
  contact_link: "bg-success/20 text-success",
};

export function LinksPage() {
  const [linkType, setLinkType] = useState("");
  const [status, setStatus] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["wa-links", linkType, status],
    queryFn: () => api.waLinks({ linkType: linkType || undefined, status: status || undefined }),
  });

  const stats = useQuery({
    queryKey: ["wa-link-stats"],
    queryFn: () => api.waLinkStats(),
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div className="flex gap-6">
      <div className="flex-1">
        <Header title="Discovered Links" subtitle="WhatsApp link extraction" onRefresh={() => refetch()} />
        <div className="flex items-center gap-3 mb-4">
          <FilterDropdown label="Type" value={linkType} onChange={setLinkType} options={typeOptions} />
          <FilterDropdown label="Status" value={status} onChange={setStatus} options={statusOptions} />
        </div>
        <div className="bg-surface rounded-lg border border-border p-4">
          {isLoading ? <LoadingSpinner /> : (
            <table className="w-full text-sm">
              <thead><tr className="text-left text-text-muted border-b border-border">
                <th className="pb-2">Link</th><th className="pb-2">Type</th><th className="pb-2">Status</th><th className="pb-2">Discovered</th>
              </tr></thead>
              <tbody>
                {data?.map((l) => {
                  const href = l.url || l.link;
                  return (
                    <tr key={l.id} className="border-b border-border/50 hover:bg-white/5">
                      <td className="py-2 truncate max-w-[300px]"><a href={href} target="_blank" rel="noreferrer" className="text-info hover:underline">{href}</a></td>
                      <td className="py-2"><span className={`text-xs px-1.5 py-0.5 rounded ${typeBadge[l.link_type] ?? "bg-text-muted/20 text-text-muted"}`}>{l.link_type}</span></td>
                      <td className="py-2 text-xs">{l.status}</td>
                      <td className="py-2 text-text-muted">{formatTimestamp(l.discovered_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
      <div className="w-56 shrink-0">
        <h3 className="text-sm font-semibold mb-3">Stats</h3>
        {stats.isLoading ? <LoadingSpinner /> : (
          <div className="space-y-2">
            {stats.data?.map((s, i) => (
              <div key={i} className="bg-surface border border-border rounded-lg p-3">
                <p className="text-xs text-text-muted">{s.link_type} &middot; {s.status}</p>
                <p className="text-lg font-semibold font-mono">{s.count}</p>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
