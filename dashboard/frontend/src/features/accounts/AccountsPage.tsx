import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
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

      {/* Cookie-auth sources */}
      <section className="bg-surface border border-border rounded-lg p-4">
        <h3 className="text-sm font-medium mb-3">Cookie sources</h3>
        <table className="w-full text-sm">
          <thead><tr className="text-left text-text-muted border-b border-border">
            <th className="pb-2">Source</th><th className="pb-2">Health</th><th className="pb-2">Cookie</th><th className="pb-2">Age</th><th className="pb-2">Last success</th>
          </tr></thead>
          <tbody>
            {data.cookies.map((c) => {
              const stale = c.cookie_age_days != null && c.cookie_age_days > 30;
              return (
                <tr key={c.source} className="border-b border-border/50">
                  <td className="py-2 uppercase text-xs">{c.source}</td>
                  <td className="py-2">
                    <span className="inline-flex items-center gap-1.5">
                      <StatusDot ok={c.health === "running"} warn={c.health === "degraded" || c.health === "auth_paused"} />
                      <span className="text-text-muted text-xs">{c.health}</span>
                    </span>
                  </td>
                  <td className="py-2 text-text-muted text-xs">{c.has_cookie ? c.cookie_file : <span className="text-danger">missing</span>}</td>
                  <td className={`py-2 text-xs ${stale ? "text-warning font-medium" : "text-text-muted"}`}>
                    {c.cookie_age_days != null ? `${c.cookie_age_days}d${stale ? " ⚠ refresh" : ""}` : "—"}
                  </td>
                  <td className="py-2 text-text-muted text-xs">{c.last_success_at ? relativeTime(c.last_success_at) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="mt-3 text-[11px] text-text-muted">
          Cookies are refreshed automatically by the browser extension's live-cookie sync; a large age here means that
          source hasn't been re-synced recently. Drop a new cookie file in <code>credentials/&lt;source&gt;/</code> to update manually.
        </p>
      </section>
    </div>
  );
}
