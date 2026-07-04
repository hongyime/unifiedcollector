import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import { Header } from "../../components/layout/Header";
import { LoadingSpinner } from "../../components/ui/LoadingSpinner";
import { ErrorState } from "../../components/ui/ErrorState";

function formatDistance(m: number | null): string {
  if (m == null) return "—";
  if (m >= 1000) return `${(m / 1000).toFixed(2)} km`;
  return `${Math.round(m)} m`;
}

function formatDuration(s: number | null): string {
  if (s == null) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
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
    dates.refetch();
    if (date) activities.refetch();
  };

  const dateOptions = useMemo(() => dates.data ?? [], [dates.data]);

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
            {stats.isLoading ? "…" : formatDistance(stats.data?.total_distance ?? null)}
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
                <tr key={act.platform_activity_id} className="border-b border-border/50 hover:bg-white/5">
                  <td className="py-2">
                    <div className="flex items-center gap-2">
                      {act.profile ? (
                        <img src={act.profile} alt="" className="w-6 h-6 rounded-full" />
                      ) : (
                        <div className="w-6 h-6 rounded-full bg-white/10" />
                      )}
                      <span>{athleteLabel(act)}</span>
                    </div>
                  </td>
                  <td className="py-2">
                    <a
                      href={`https://www.strava.com/activities/${act.platform_activity_id}`}
                      target="_blank"
                      rel="noreferrer"
                      className="hover:underline"
                    >
                      {act.name ?? "(no name)"}
                    </a>
                  </td>
                  <td className="py-2 text-xs uppercase">{act.sport_type ?? act.type ?? "—"}</td>
                  <td className="py-2">{formatDistance(act.distance)}</td>
                  <td className="py-2">{formatDuration(act.moving_time ?? act.elapsed_time)}</td>
                  <td className="py-2">
                    {act.total_elevation_gain != null ? `${Math.round(act.total_elevation_gain)} m` : "—"}
                  </td>
                  <td className="py-2 text-text-muted text-xs">
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
