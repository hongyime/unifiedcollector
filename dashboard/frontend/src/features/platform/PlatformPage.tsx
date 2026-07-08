import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { relativeTime } from "../../utils/formatters";

// Deep-dive links to the specialized pages that still exist for some platforms.
const SPECIALIZED: Record<string, { to: string; label: string }[]> = {
  strava: [{ to: "/strava/feed", label: "Activity feed" }],
  telegram: [{ to: "/telegram/accounts", label: "Onboard accounts" }, { to: "/telegram/stats", label: "Stats" }],
  whatsapp: [{ to: "/whatsapp/link", label: "Link device" }, { to: "/whatsapp/links", label: "Links" }],
  instagram: [{ to: "/instagram/dms", label: "Messages" }],
  tiktok: [{ to: "/tiktok/dms", label: "Messages" }],
};

function Stat({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="bg-background border border-border rounded-md px-3 py-2 min-w-[110px]">
      <div className="text-[11px] uppercase text-text-muted">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{typeof value === "number" ? value.toLocaleString() : value}</div>
      {sub && <div className="text-[11px] text-text-muted">{sub}</div>}
    </div>
  );
}

// One generic page for any platform (route /platform/:name) — shows what's been
// collected for that platform: recent media (what was just scraped), counts, the
// per-account follow graph, and live status. Keeps every platform consistent.
export function PlatformPage() {
  const { name = "" } = useParams();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["platform", name],
    queryFn: () => api.platformSummary(name),
    refetchInterval: 20_000,
  });

  if (error) return <ErrorState message={String(error)} onRetry={() => refetch()} />;
  if (isLoading || !data) return <LoadingSpinner />;

  const liveColor = data.live === "live" ? "bg-success" : data.live === "stale" || data.live === "degraded" ? "bg-warning" : data.live === "dead" ? "bg-danger" : "bg-text-muted";
  const lastActivity = data.media_last || data.messages_last;

  return (
    <div>
      <Header
        title={name.charAt(0).toUpperCase() + name.slice(1)}
        subtitle="Collected data & recent activity"
        onRefresh={() => refetch()}
      />

      {/* status + stats */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <span className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-md bg-surface border border-border">
          <span className={`w-2 h-2 rounded-full ${liveColor}`} />
          {data.live ?? "unknown"}{lastActivity ? ` · last ${relativeTime(lastActivity)}` : ""}
        </span>
      </div>
      <div className="flex flex-wrap gap-2 mb-6">
        <Stat label="Media" value={data.media_count} />
        <Stat label="People" value={data.users_count} />
        {data.posts_count != null && <Stat label={data.posts_label || "Posts"} value={data.posts_count} />}
        {data.messages_count != null && <Stat label="Messages" value={data.messages_count} />}
      </div>

      {/* per-account follow graph */}
      {data.follow_edges.length > 0 && (
        <section className="bg-surface border border-border rounded-lg p-4 mb-6">
          <h3 className="text-sm font-medium mb-2">Per-account follow graph</h3>
          <div className="flex flex-wrap gap-2">
            {data.follow_edges.map((e) => (
              <div key={e.owner_account} className="bg-background border border-border rounded-md px-3 py-2">
                <div className="text-xs">{e.owner_account}</div>
                <div className="text-[11px] text-text-secondary tabular-nums">{e.followers.toLocaleString()} followers · {e.following.toLocaleString()} following</div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* just collected — recent media */}
      <section className="bg-surface border border-border rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Just collected</h3>
          <Link to={`/browse?source=${name}`} className="text-xs text-info hover:underline">View all media →</Link>
        </div>
        {data.media_recent.length === 0 ? (
          <p className="text-sm text-text-muted py-4 text-center">No media collected yet.</p>
        ) : (
          <div className="grid grid-cols-6 gap-2">
            {data.media_recent.map((m) => (
              <div key={m.id} className="group relative aspect-square rounded-md overflow-hidden bg-background border border-border" title={`${m.entity_name} · ${relativeTime(m.collected_at)}`}>
                <img src={api.thumbnailUrl(m.id)} alt={m.filename} loading="lazy" className="w-full h-full object-cover" />
                <div className="absolute bottom-0 inset-x-0 bg-black/60 text-white text-[9px] px-1 py-0.5 truncate opacity-0 group-hover:opacity-100">{m.entity_name}</div>
              </div>
            ))}
          </div>
        )}
        <div className="mt-3 flex gap-3 text-xs">
          <Link to={`/social/users`} className="text-info hover:underline">People →</Link>
          {SPECIALIZED[name]?.map((s) => (
            <Link key={s.to} to={s.to} className="text-info hover:underline">{s.label} →</Link>
          ))}
        </div>
      </section>
    </div>
  );
}
