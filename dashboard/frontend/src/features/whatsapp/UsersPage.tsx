import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";

export function UsersPage() {
  const [search, setSearch] = useState("");
  const [expandedJid, setExpandedJid] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["wa-users", search],
    queryFn: () => api.waUsers(search || undefined),
  });

  const history = useQuery({
    queryKey: ["wa-user-history", expandedJid],
    queryFn: () => api.waUserHistory(expandedJid!),
    enabled: !!expandedJid,
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  return (
    <div>
      <Header title="WhatsApp Users" subtitle="Discovered contacts" onRefresh={() => refetch()} />
      <div className="mb-4">
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search users..." className="bg-background border border-border rounded-md text-sm px-3 py-1.5 w-64" />
      </div>
      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">JID</th><th className="pb-2">Push Name</th><th className="pb-2">Display Name</th><th className="pb-2">Phone</th><th className="pb-2">Business</th><th className="pb-2">Messages</th><th className="pb-2">Last Seen</th>
            </tr></thead>
            <tbody>
              {data?.map((u) => (
                <>
                  <tr key={u.jid} onClick={() => setExpandedJid(expandedJid === u.jid ? null : u.jid)} className="border-b border-border/50 hover:bg-white/5 cursor-pointer">
                    <td className="py-2 font-mono text-xs">{u.jid}</td>
                    <td className="py-2">{u.push_name ?? "-"}</td>
                    <td className="py-2">{u.display_name ?? "-"}</td>
                    <td className="py-2 text-text-muted">{u.phone_number ?? "-"}</td>
                    <td className="py-2">{u.is_business ? <span className="text-xs bg-info/20 text-info px-1.5 py-0.5 rounded">Biz</span> : "-"}</td>
                    <td className="py-2">{u.message_count}</td>
                    <td className="py-2 text-text-muted">{relativeTime(u.last_seen)}</td>
                  </tr>
                  {expandedJid === u.jid && (
                    <tr key={`${u.jid}-history`}>
                      <td colSpan={7} className="bg-background p-3">
                        {history.isLoading ? <LoadingSpinner /> : history.data?.length ? (
                          <div className="space-y-1">
                            {history.data.map((h) => (
                              <div key={h.id} className="flex items-center gap-3 text-xs">
                                <span className="text-text-muted w-24 shrink-0">{relativeTime(h.changed_at)}</span>
                                <span className="font-medium">{h.field_name}</span>
                                <span className="text-error line-through">{h.old_value}</span>
                                <span>&rarr;</span>
                                <span className="text-success">{h.new_value}</span>
                              </div>
                            ))}
                          </div>
                        ) : <p className="text-xs text-text-muted">No history</p>}
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
