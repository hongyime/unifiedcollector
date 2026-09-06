import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { AuthImage } from "../../components/ui/AuthImage";
import { relativeTime } from "../../utils/formatters";

// Cross-platform social registry (social_users), replacing the WhatsApp-only Users
// view. Shows everyone we've seen on any platform, filterable by platform + search.
export function SocialUsersPage() {
  const [platform, setPlatform] = useState("");
  const [search, setSearch] = useState("");

  const network = useQuery({ queryKey: ["social-network"], queryFn: () => api.socialNetwork() });
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["social-users", platform, search],
    queryFn: () => api.socialUsers({ platform: platform || undefined, q: search || undefined, limit: 100 }),
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;

  const platforms = (network.data ?? []).filter((n) => n.total > 0);

  return (
    <div>
      <Header title="Social Users" subtitle="Cross-platform registry (social_users)" onRefresh={() => refetch()} />

      <div className="flex flex-wrap items-center gap-2 mb-4">
        <button
          onClick={() => setPlatform("")}
          className={`text-xs px-2.5 py-1 rounded-md border ${platform === "" ? "bg-info/20 border-info text-info" : "border-border text-text-muted hover:bg-white/5"}`}
        >All</button>
        {platforms.map((p) => (
          <button
            key={p.platform}
            onClick={() => setPlatform(p.platform)}
            className={`text-xs px-2.5 py-1 rounded-md border uppercase ${platform === p.platform ? "bg-info/20 border-info text-info" : "border-border text-text-muted hover:bg-white/5"}`}
          >{p.platform} <span className="tabular-nums opacity-60">{p.total.toLocaleString()}</span></button>
        ))}
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search name / username…"
          className="ml-auto bg-background border border-border rounded-md text-sm px-3 py-1.5 w-64"
        />
      </div>

      <div className="bg-surface rounded-lg border border-border p-4">
        {isLoading ? <LoadingSpinner /> : !data?.length ? (
          <p className="text-sm text-text-muted py-6 text-center">No users match.</p>
        ) : (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">Platform</th><th className="pb-2">User</th><th className="pb-2">Contexts</th><th className="pb-2 text-right">Seen</th><th className="pb-2">Last seen</th>
            </tr></thead>
            <tbody>
              {data.map((u) => (
                <tr key={`${u.platform}-${u.uid}`} className="border-b border-border/50 hover:bg-white/5">
                  <td className="py-2 uppercase text-xs text-text-muted">{u.platform}</td>
                  <td className="py-2">
                    <div className="flex items-center gap-2">
                      {u.profile_photo_url ? (
                        <AuthImage
                          src={u.profile_photo_url}
                          alt=""
                          className="w-6 h-6 rounded-full object-cover bg-background shrink-0"
                          fallbackLabel={u.platform}
                        />
                      ) : (
                        <span className="w-6 h-6 rounded-full bg-background border border-border shrink-0" />
                      )}
                      <div className="min-w-0">
                        <div className="truncate">{u.display_name || u.username || <span className="text-text-muted">#{u.uid}</span>}</div>
                        {u.username && u.display_name && <div className="text-[11px] text-text-muted truncate">@{u.username}</div>}
                      </div>
                    </div>
                  </td>
                  <td className="py-2">
                    <div className="flex flex-wrap gap-1">
                      {(u.contexts ?? []).map((c) => (
                        <span key={c} className="text-[10px] px-1.5 py-0.5 rounded bg-background border border-border text-text-muted">{c}</span>
                      ))}
                    </div>
                  </td>
                  <td className="py-2 text-right tabular-nums text-text-muted">{u.times_seen.toLocaleString()}</td>
                  <td className="py-2 text-text-muted">{u.last_seen ? relativeTime(u.last_seen) : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
