import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";

function StatusDot({ ok, warn }: { ok: boolean; warn?: boolean }) {
  return <span className={`inline-block w-2 h-2 rounded-full ${ok ? "bg-success" : warn ? "bg-warning" : "bg-danger"}`} />;
}

// Cross-platform account/session panel (replaces the telegram-only Accounts view):
// telegram MTProto accounts, whatsapp bridge devices, and cookie-auth sources with
// cookie freshness + health.
export function AccountsPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.accounts(),
    refetchInterval: 15_000,
  });
  const edges = useQuery({
    queryKey: ["follow-edges-stats"],
    queryFn: () => api.followEdgesStats(),
    refetchInterval: 30_000,
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;
  if (isLoading || !data) return <LoadingSpinner />;

  return (
    <div>
      <Header title="Accounts" subtitle="All platforms — sessions, devices & cookies" onRefresh={() => refetch()} />

      {/* Telegram MTProto accounts */}
      <section className="bg-surface border border-border rounded-lg p-4 mb-4">
        <h3 className="text-sm font-medium mb-3">Telegram <span className="text-text-muted">· {data.telegram.length} accounts</span></h3>
        <div className="grid grid-cols-2 gap-2">
          {data.telegram.map((a) => (
            <div key={a.name} className="bg-background border border-border rounded-md px-3 py-2 flex items-center gap-2">
              <StatusDot ok={(a.status ?? "").toLowerCase().includes("connect") || a.status === "active"} warn />
              <div className="min-w-0">
                <div className="text-sm truncate">{a.name}</div>
                <div className="text-[11px] text-text-muted truncate">{a.phone ?? "—"} · {a.status ?? "unknown"}{a.last_connected_at ? ` · ${relativeTime(a.last_connected_at)}` : ""}</div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* WhatsApp bridge devices */}
      <section className="bg-surface border border-border rounded-lg p-4 mb-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">WhatsApp <span className="text-text-muted">· {data.whatsapp.length} devices</span></h3>
          <Link to="/whatsapp/link" className="text-xs text-info hover:underline">Link / manage devices →</Link>
        </div>
        <div className="grid grid-cols-2 gap-2">
          {data.whatsapp.map((w) => (
            <div key={w.session} className="bg-background border border-border rounded-md px-3 py-2 flex items-center gap-2">
              <StatusDot ok={w.ready} warn={w.status === "awaiting_scan"} />
              <div>
                <div className="text-sm">Device {w.session}</div>
                <div className="text-[11px] text-text-muted">{w.status}</div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Per-account follow graph (follow_edges) */}
      {(edges.data ?? []).length > 0 && (
        <section className="bg-surface border border-border rounded-lg p-4 mb-4">
          <h3 className="text-sm font-medium mb-3">Per-account follow graph <span className="text-text-muted">· who follows me / who I follow, per account</span></h3>
          <table className="w-full text-sm">
            <thead><tr className="text-left text-text-muted border-b border-border">
              <th className="pb-2">Platform</th><th className="pb-2">Account</th><th className="pb-2 text-right">Followers</th><th className="pb-2 text-right">Following</th><th className="pb-2">Last capture</th>
            </tr></thead>
            <tbody>
              {(edges.data ?? []).map((e) => (
                <tr key={`${e.platform}-${e.owner_account}`} className="border-b border-border/50">
                  <td className="py-2 uppercase text-xs text-text-muted">{e.platform}</td>
                  <td className="py-2 text-xs">{e.owner_account}</td>
                  <td className="py-2 text-right tabular-nums">{e.followers.toLocaleString()}</td>
                  <td className="py-2 text-right tabular-nums">{e.following.toLocaleString()}</td>
                  <td className="py-2 text-text-muted text-xs">{e.last_seen ? relativeTime(e.last_seen) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[11px] text-text-muted">Captured per account via the extension self-seed — switch to an account in your browser to capture its graph. (The extension can't auto-switch accounts.)</p>
        </section>
      )}

      {/* Cookie-auth sources */}
      <section className="bg-surface border border-border rounded-lg p-4">
        <h3 className="text-sm font-medium mb-3">Cookie sources</h3>
        <table className="w-full text-sm">
          <thead><tr className="text-left text-text-muted border-b border-border">
            <th className="pb-2">Source</th><th className="pb-2">Account</th><th className="pb-2">Live</th><th className="pb-2">Age</th><th className="pb-2">Status</th>
          </tr></thead>
          <tbody>
            {[...data.cookies].sort((a, b) => Number(b.needs_refresh) - Number(a.needs_refresh)).map((c) => (
              <tr key={`${c.source}-${c.file}`} className={`border-b border-border/50 ${c.needs_refresh ? "bg-danger/5" : ""}`}>
                <td className="py-2 uppercase text-xs">{c.source}</td>
                <td className="py-2 text-xs">{c.account}</td>
                <td className="py-2">
                  <span className="inline-flex items-center gap-1.5">
                    <StatusDot ok={c.live_status === "ok"} warn={c.live_status == null || c.live_status === "unknown"} />
                    <span className="text-text-muted text-xs">{c.live_status ?? "untested"}</span>
                  </span>
                </td>
                <td className={`py-2 text-xs ${c.age_days != null && c.age_days > 30 ? "text-warning" : "text-text-muted"}`}>
                  {c.age_days != null ? `${c.age_days}d` : "—"}
                </td>
                <td className="py-2 text-xs">
                  {c.needs_refresh ? (
                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-danger/20 text-danger font-medium">
                      ⚠ refresh<span className="text-danger/70 font-normal">— {c.reason}</span>
                    </span>
                  ) : (
                    <span className="text-success">ok</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-3 text-[11px] text-text-muted">
          "Live" = the collector's actual last auth result (tested every cycle); "Age" = cookie file age. A row is flagged
          <span className="text-danger"> ⚠ refresh</span> when it's 401-dead, expired, missing its session cookie, or stale (&gt;30d).
          Refresh by dropping a new cookie file in <code>credentials/&lt;source&gt;/</code> (or let the extension's live-cookie sync do it).
        </p>
      </section>
    </div>
  );
}
