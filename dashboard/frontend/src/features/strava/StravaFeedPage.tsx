import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Clock3, ExternalLink, Map as MapIcon, MousePointer2, ShieldOff } from "lucide-react";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";
import { AuthImage } from "../../components/ui/AuthImage";
import { decodePolyline, polylineToSvgPath } from "./polyline";

// `distance` is meters (Strava API convention); `distance_unit` is the athlete's
// display preference ('mi' | 'km'). Convert on render so the value matches what
// the athlete sees on strava.com. Default 'mi' matches the DB column default.
function formatDistance(m: number | null, unit: string | null): string {
  if (m == null) return "—";
  const u = (unit || "mi").toLowerCase();
  if (u === "km") return `${(m / 1000).toFixed(2)} km`;
  return `${(m / 1609.344).toFixed(2)} mi`;
}

// Total-stats row can't know per-athlete unit — default to km there, keep
// per-row formatting unit-aware.
function formatDistanceKm(m: number | null): string {
  if (m == null) return "—";
  if (m >= 1000) return `${(m / 1000).toFixed(2)} km`;
  return `${Math.round(m)} m`;
}

function formatCount(n: number | null | undefined): string {
  return Number(n ?? 0).toLocaleString();
}

function formatCaptureAge(iso: string | null | undefined): string {
  if (!iso) return "none yet";
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "just now";
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// mm:ss for sub-hour, h:mm:ss for longer. Matches what Strava shows for a run.
function formatDuration(s: number | null): string {
  if (s == null) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (h > 0) return `${h}:${pad(m)}:${pad(sec)}`;
  return `${m}:${pad(sec)}`;
}

// Emoji icon per activity type. Broad synonyms — Strava has ~40 sport_type
// values, most collapse to a handful of glyphs. Falls back to a compass.
function activityIcon(type: string | null, sportType: string | null): string {
  const t = (sportType || type || "").toLowerCase();
  if (t.includes("trailrun")) return "🥾";
  if (t.includes("run")) return "🏃";
  if (t.includes("walk") || t.includes("hike")) return "🚶";
  if (t.includes("ride") || t.includes("cycle") || t.includes("bike")) return "🚴";
  if (t.includes("swim")) return "🏊";
  if (t.includes("yoga")) return "🧘";
  if (t.includes("row")) return "🚣";
  if (t.includes("ski") || t.includes("snowboard")) return "⛷️";
  if (t.includes("workout") || t.includes("weight") || t.includes("crossfit")) return "🏋️";
  if (t.includes("kayak") || t.includes("canoe") || t.includes("paddle")) return "🛶";
  if (t.includes("golf")) return "⛳";
  if (t.includes("climb")) return "🧗";
  return "🧭";
}

// Fixed thumbnail size — small enough to sit inside a table cell, big enough
// to convey the route shape. Padding leaves the stroke away from the edge so
// endpoints aren't clipped. viewBox is set to the same values so 1 SVG unit
// == 1 px and stroke widths look consistent regardless of surrounding zoom.
const MAP_W = 120;
const MAP_H = 80;

function MapThumb({
  polyline,
  streamStatus,
  routeStatus,
  routeDetail,
  startLatlng,
}: {
  polyline: string | null;
  streamStatus: string | null;
  routeStatus: string | null;
  routeDetail: string | null;
  startLatlng: string | null;
}) {
  // Decode once per polyline (cached across renders inside the table via memo
  // on the row payload — decodePolyline is pure so useMemo not required at
  // this scale, but the SVG-path derivation IS memo-worthy for very long
  // tracks).
  const path = useMemo(() => {
    if (!polyline) return null;
    const pts = decodePolyline(polyline);
    return polylineToSvgPath(pts, MAP_W, MAP_H);
  }, [polyline]);

  if (!path) {
    // NULL stream_status means the collector has not reached a definitive route
    // result yet. Do not label that as "no route"; those rows are still queued
    // for cookie-authenticated stream backfill.
    const label =
      routeStatus === "rate_limited" ? "429 cooldown" :
      routeStatus === "recent_429" ? "429 seen" :
      routeStatus === "privacy_zone" ? "privacy zone" :
      routeStatus === "no_gps" ? "no gps" :
      routeStatus === "unverifiable" ? "unverified" :
      routeStatus === "start_only" ? "start only" :
      routeStatus === "queued" ? "route queued" :
      streamStatus === "truncated_empty" ? "privacy zone" :
      streamStatus === "incomplete" ? "no gps" :
      streamStatus == null ? "route queued" :
      startLatlng ? "start only" : "no route";
    return (
      <div
        className="flex items-center justify-center rounded border border-dashed border-border/60 bg-black/20 text-[9px] uppercase tracking-wider text-text-muted"
        style={{ width: MAP_W, height: MAP_H }}
        title={routeDetail ?? undefined}
      >
        {label}
      </div>
    );
  }
  return (
    <svg
      width={MAP_W}
      height={MAP_H}
      viewBox={`0 0 ${MAP_W} ${MAP_H}`}
      className="rounded border border-border/60 bg-black/30"
      role="img"
      aria-label="route thumbnail"
    >
      <path d={path} fill="none" stroke="#fc4c02" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

// The signed-in strava owner ("you") — the athlete the collector authenticates as
// (resolved athlete_id=72101656, ~2939 own activities). Shown as "★ Me" so you
// recognise yourself instead of a bare id / the "Strava" placeholder name.
const STRAVA_OWNER_ID = 72101656;

function athleteLabel(a: { username: string | null; firstname: string | null; lastname: string | null; platform_athlete_id?: number | null }): string {
  const id = a.platform_athlete_id;
  if (id != null && id === STRAVA_OWNER_ID) return "★ Me";
  const idStr = id != null ? String(id) : "";
  // Reject numeric-id placeholders that were backfilled into name fields for
  // athletes whose real name strava never exposed.
  const clean = (v: string | null) => (v && v !== idStr ? v : "");
  const name = `${clean(a.firstname)} ${clean(a.lastname)}`.trim();
  if (name) return name;
  if (clean(a.username)) return a.username as string;
  return id != null ? `Unknown #${id}` : "Unknown";
}

export function StravaFeedPage() {
  const [athleteId, setAthleteId] = useState<number | undefined>(undefined);
  const [date, setDate] = useState<string>("");

  const athletes = useQuery({
    queryKey: ["strava-athletes"],
    queryFn: () => api.stravaAthletes(),
  });

  const stats = useQuery({
    queryKey: ["strava-feed-stats", athleteId],
    queryFn: () => api.stravaFeedStats(athleteId),
  });

  const routeQueue = useQuery({
    queryKey: ["strava-route-capture-queue"],
    queryFn: () => api.stravaRouteCaptureQueue(8),
  });

  const dates = useQuery({
    queryKey: ["strava-feed-dates", athleteId],
    queryFn: () => api.stravaFeedDates(athleteId),
  });

  const activities = useQuery({
    queryKey: ["strava-feed-activities", athleteId, date],
    queryFn: () => api.stravaFeedActivities(date, athleteId),
    enabled: !!date,
  });

  const refresh = () => {
    athletes.refetch();
    stats.refetch();
    routeQueue.refetch();
    dates.refetch();
    if (date) activities.refetch();
  };

  const dateOptions = useMemo(() => dates.data ?? [], [dates.data]);
  const coverage = stats.data?.route_coverage;
  const queueItems = routeQueue.data?.items ?? [];

  if (athletes.error) return <ErrorState message={String(athletes.error)} onRetry={refresh} />;

  return (
    <div>
      <Header
        title="Strava Feed"
        subtitle="Following-feed historical playback"
        onRefresh={refresh}
      />

      {/* Stats */}
      <div className="grid grid-cols-4 gap-3 mb-4">
        <div className="bg-surface border border-border rounded-lg p-3">
          <div className="text-[10px] uppercase text-text-muted mb-1">Activities</div>
          <div className="text-2xl font-semibold">
            {stats.isLoading ? "…" : (stats.data?.total_activities ?? 0)}
          </div>
        </div>
        <div className="bg-surface border border-border rounded-lg p-3">
          <div className="text-[10px] uppercase text-text-muted mb-1">Distance</div>
          <div className="text-2xl font-semibold">
            {stats.isLoading ? "…" : formatDistanceKm(stats.data?.total_distance ?? null)}
          </div>
        </div>
        <div className="bg-surface border border-border rounded-lg p-3">
          <div className="text-[10px] uppercase text-text-muted mb-1">Moving Time</div>
          <div className="text-2xl font-semibold">
            {stats.isLoading ? "…" : formatDuration(stats.data?.total_moving_time ?? null)}
          </div>
        </div>
        <div className="bg-surface border border-border rounded-lg p-3">
          <div className="text-[10px] uppercase text-text-muted mb-1">Elevation</div>
          <div className="text-2xl font-semibold">
            {stats.isLoading ? "…" : `${Math.round(stats.data?.total_elevation_gain ?? 0)} m`}
          </div>
        </div>
      </div>

      {coverage ? (
        <div className="grid grid-cols-2 xl:grid-cols-6 gap-3 mb-4">
          <div className="bg-surface border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 text-[10px] uppercase text-text-muted mb-1">
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
              Mapped
            </div>
            <div className="text-xl font-semibold">{formatCount(coverage.mapped)}</div>
            <div className="text-[11px] text-text-muted">{coverage.completion_pct.toFixed(1)}% of {formatCount(coverage.total)}</div>
          </div>
          <div className="bg-surface border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 text-[10px] uppercase text-text-muted mb-1">
              <MousePointer2 className="w-3.5 h-3.5 text-sky-400" />
              Browser Captured
            </div>
            <div className="text-xl font-semibold">{formatCount(coverage.browser_captured)}</div>
            <div className="text-[11px] text-text-muted">last {formatCaptureAge(coverage.latest_browser_capture_at)}</div>
          </div>
          <div className="bg-surface border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 text-[10px] uppercase text-text-muted mb-1">
              <Clock3 className="w-3.5 h-3.5 text-amber-300" />
              Pending
            </div>
            <div className="text-xl font-semibold">{formatCount(coverage.queued + coverage.start_only)}</div>
            <div className="text-[11px] text-text-muted">{formatCount(coverage.queued)} queued · {formatCount(coverage.start_only)} start-only</div>
          </div>
          <div className="bg-surface border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 text-[10px] uppercase text-text-muted mb-1">
              <AlertTriangle className="w-3.5 h-3.5 text-orange-300" />
              GPS 429
            </div>
            <div className="text-xl font-semibold">{formatCount(coverage.recent_gps_429_events)}</div>
            <div className="text-[11px] text-text-muted truncate" title={coverage.active_gps_cooldown_reason ?? undefined}>
              {coverage.active_gps_cooldown_until ? `cooldown until ${new Date(coverage.active_gps_cooldown_until).toLocaleTimeString()}` : "no active cooldown"}
            </div>
          </div>
          <div className="bg-surface border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 text-[10px] uppercase text-text-muted mb-1">
              <ShieldOff className="w-3.5 h-3.5 text-rose-300" />
              Hidden / No GPS
            </div>
            <div className="text-xl font-semibold">{formatCount(coverage.privacy_zone + coverage.no_gps)}</div>
            <div className="text-[11px] text-text-muted">{formatCount(coverage.privacy_zone)} hidden · {formatCount(coverage.no_gps)} no GPS</div>
          </div>
          <div className="bg-surface border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 text-[10px] uppercase text-text-muted mb-1">
              <MapIcon className="w-3.5 h-3.5 text-text-muted" />
              Unverified
            </div>
            <div className="text-xl font-semibold">{formatCount(coverage.unverifiable)}</div>
            <div className="text-[11px] text-text-muted">old route rows skipped safely</div>
          </div>
        </div>
      ) : null}

      <div className="bg-surface border border-border rounded-lg p-4 mb-4">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold">
              <MapIcon className="w-4 h-4 text-orange-400" />
              Route capture queue
            </div>
            <div className="text-xs text-text-muted mt-1">
              {routeQueue.data?.cooldown.active
                ? `Cooldown until ${routeQueue.data.cooldown.until ? new Date(routeQueue.data.cooldown.until).toLocaleTimeString() : "later"}`
                : `${queueItems.length} next candidate${queueItems.length === 1 ? "" : "s"}`}
            </div>
          </div>
          <div className="text-xs text-text-muted">
            Revisit TTL: {routeQueue.data?.recent_visit_ttl_hours ?? 6}h
          </div>
        </div>

        {routeQueue.isLoading ? (
          <LoadingSpinner />
        ) : routeQueue.error ? (
          <ErrorState message={String(routeQueue.error)} onRetry={() => routeQueue.refetch()} />
        ) : routeQueue.data?.cooldown.active ? (
          <div
            className="rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-100"
            title={routeQueue.data.cooldown.reason ?? undefined}
          >
            {routeQueue.data.cooldown.reason ?? "Strava GPS stream cooldown is active"}
          </div>
        ) : queueItems.length === 0 ? (
          <div className="text-sm text-text-muted">No eligible route candidates right now.</div>
        ) : (
          <div className="divide-y divide-border/60">
            {queueItems.map((item) => (
              <div key={item.platform_activity_id} className="flex items-center justify-between gap-3 py-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-medium">{item.name ?? "(no name)"}</span>
                    <span className="rounded border border-border/80 px-1.5 py-0.5 text-[10px] uppercase text-text-muted">
                      tier {item.proximity_tier}
                    </span>
                    {item.target_priority > 0 ? (
                      <span className="rounded border border-orange-400/40 px-1.5 py-0.5 text-[10px] uppercase text-orange-200">
                        p{item.target_priority}
                      </span>
                    ) : null}
                  </div>
                  <div className="text-xs text-text-muted mt-1">
                    {item.athlete_name ?? "Unknown athlete"} · {item.start_date ? new Date(item.start_date).toLocaleDateString() : "no date"} · {item.sport_type ?? item.type ?? "activity"}
                  </div>
                </div>
                <a
                  href={item.activity_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded border border-border hover:bg-white/5"
                  title="Open activity"
                  aria-label="Open activity"
                >
                  <ExternalLink className="h-4 w-4" />
                </a>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3 mb-4">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-text-muted">Athlete:</span>
          <select
            className="bg-surface border border-border rounded px-2 py-1 text-sm"
            value={athleteId ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              setAthleteId(v === "" ? undefined : Number(v));
              setDate("");
            }}
          >
            <option value="">All athletes</option>
            {(athletes.data ?? []).map((a) => (
              <option key={a.platform_athlete_id} value={a.platform_athlete_id}>
                {athleteLabel(a)} ({a.activity_count})
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-text-muted">Date:</span>
          <select
            className="bg-surface border border-border rounded px-2 py-1 text-sm"
            value={date}
            onChange={(e) => setDate(e.target.value)}
          >
            <option value="">Pick a date…</option>
            {dateOptions.map((d) => (
              <option key={d.date} value={d.date}>
                {d.date} ({d.count})
              </option>
            ))}
          </select>
        </label>
      </div>

      {/* Activity list */}
      <div className="bg-surface border border-border rounded-lg p-4">
        {!date ? (
          <div className="text-sm text-text-muted">
            Select a date to view activities. Total dates with data: {dateOptions.length}
          </div>
        ) : activities.isLoading ? (
          <LoadingSpinner />
        ) : activities.error ? (
          <ErrorState message={String(activities.error)} onRetry={() => activities.refetch()} />
        ) : !activities.data || activities.data.length === 0 ? (
          <div className="text-sm text-text-muted">No activities on {date}.</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-text-muted border-b border-border">
                <th className="pb-2">Athlete</th>
                <th className="pb-2">Map</th>
                <th className="pb-2">Activity</th>
                <th className="pb-2">Type</th>
                <th className="pb-2">Distance</th>
                <th className="pb-2">Time</th>
                <th className="pb-2">Elevation</th>
                <th className="pb-2">Started</th>
              </tr>
            </thead>
            <tbody>
              {activities.data.map((act) => (
                <tr key={act.platform_activity_id} className="border-b border-border/50 hover:bg-white/5 align-middle">
                  <td className="py-2">
                    <div className="flex items-center gap-2">
                      {act.profile ? (
                        <AuthImage
                          src={act.profile}
                          alt=""
                          className="w-6 h-6 rounded-full object-cover shrink-0 bg-background"
                          fallbackLabel="st"
                        />
                      ) : (
                        <div className="w-6 h-6 rounded-full bg-white/10" />
                      )}
                      <span>{athleteLabel(act)}</span>
                    </div>
                  </td>
                  <td className="py-2">
                    <MapThumb
                      polyline={act.summary_polyline}
                      streamStatus={act.stream_status}
                      routeStatus={act.route_status}
                      routeDetail={act.route_status_detail}
                      startLatlng={act.start_latlng}
                    />
                  </td>
                  <td className="py-2 max-w-[280px]">
                    <a
                      href={`https://www.strava.com/activities/${act.platform_activity_id}`}
                      target="_blank"
                      rel="noreferrer"
                      className="hover:underline truncate block"
                      title={act.name ?? undefined}
                    >
                      {act.name ?? "(no name)"}
                    </a>
                  </td>
                  <td className="py-2 text-xs uppercase whitespace-nowrap">
                    <span className="mr-1" aria-hidden>{activityIcon(act.type, act.sport_type)}</span>
                    {act.sport_type ?? act.type ?? "—"}
                  </td>
                  <td className="py-2 whitespace-nowrap">{formatDistance(act.distance, act.distance_unit)}</td>
                  <td className="py-2 whitespace-nowrap tabular-nums">{formatDuration(act.moving_time ?? act.elapsed_time)}</td>
                  <td className="py-2 whitespace-nowrap">
                    {act.total_elevation_gain != null ? `${Math.round(act.total_elevation_gain)} m` : "—"}
                  </td>
                  <td className="py-2 text-text-muted text-xs whitespace-nowrap">
                    {act.start_date ? new Date(act.start_date).toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
