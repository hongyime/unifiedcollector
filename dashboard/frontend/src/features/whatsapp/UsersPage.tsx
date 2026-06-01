import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";

export function UsersPage() {
  const [search, setSearch] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["wa-users", search],
    queryFn: () => api.waUsers(search || undefined),
  });

  const history = useQuery({
    queryKey: ["wa-user-history", expandedId],
    queryFn: () => api.waUserHistory(expandedId!),
    enabled: !!expandedId,
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="WhatsApp Users" subtitle="Discovered contacts" onRefresh={() => refetch()} />
      <div className="mb-4">
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search users..." className="bg-background border border-border rounded-md text-sm px-3 py-1.5 w-64" />
      </div>
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : !data?.length ? (
          <p className="text-sm text-text-muted py-6 text-center">No WhatsApp users collected yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">JID</th><th className="pb-2">Name</th><th className="pb-2">Push Name</th><th className="pb-2">Status</th><th className="pb-2">Updated</th>
            </tr></thead>
            <tbody>
              {data?.map((u) => (
                <>
                  <tr key={u.platform_user_id} onClick={() => setExpandedId(expandedId === u.platform_user_id ? null : u.platform_user_id)} className="border-b border-border/50 hover:bg-white/5 cursor-pointer">
                    <td className="py-2 font-mono text-xs">{u.platform_user_id}</td>
                    <td className="py-2">{u.name ?? "-"}</td>
                    <td className="py-2">{u.pushname ?? "-"}</td>
                    <td className="py-2 text-text-muted">{u.status ?? u.about ?? "-"}</td>
                    <td className="py-2 text-text-muted">{relativeTime(u.updated_at)}</td>
                  </tr>
                  {expandedId === u.platform_user_id && (
                    <tr key={`${u.platform_user_id}-history`}>
                      <td colSpan={5} className="bg-background p-3">
                        {history.isLoading ? <LoadingSpinner /> : history.data?.length ? (
                          <div className="space-y-1">
                            {history.data.map((h) => (
                              <div key={h.id} className="flex items-center gap-3 text-xs">
                                <span className="text-text-muted w-24 shrink-0">{relativeTime(h.collected_at)}</span>
                                <span className="font-medium uppercase text-text-muted">{h.message_type ?? "msg"}</span>
                                <span className="truncate">{h.content ?? h.caption ?? "(media)"}</span>
                              </div>
                            ))}
                          </div>
                        ) : <p className="text-xs text-text-muted">No recent messages</p>}
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
