import { useEffect, useState } from "react";
import { useFilters } from "../context/FilterContext";
import { fetchJson } from "../lib/api";

export default function AthletesTab() {
  const { filters } = useFilters();
  const [athletes, setAthletes] = useState([]);
  const [stats, setStats] = useState({});
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    loadAthletes();
  }, []);

  async function loadAthletes() {
    setLoading(true);
    try {
      const data = await fetchJson("/api/v1/athletes");
      const list = data?.athletes || data || [];
      setAthletes(list);
      // Load stats for each athlete (fire-and-forget)
      list.slice(0, 50).forEach(async (a) => {
        try {
          const s = await fetchJson(`/api/v1/athletes/${a.athlete_id}/stats`);
          setStats((prev) => ({ ...prev, [a.athlete_id]: s }));
        } catch {}
      });
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  const filtered = athletes.filter((a) => {
    if (filters.sportType) return true; // sport filter not applicable here
    return true;
  });

  return (
    <div className="tabContent">
      {loading && <div className="tabOverlay">Loading athletes...</div>}
      <div className="athleteGrid">
        {filtered.map((a) => {
          const s = stats[a.athlete_id];
          return (
            <div key={a.athlete_id} className="athleteCard">
              {a.avatar_url && <img src={a.avatar_url} alt={a.name} className="athleteAvatar" />}
              <div className="athleteInfo">
                <div className="athleteName">{a.name}</div>
                <div className="athleteMeta">
                  {a.is_following ? <span className="badge badgeFollowing">Following</span> : null}
                  {a.activity_count ? <span className="badge">{a.activity_count} activities</span> : null}
                </div>
                {s && (
                  <div className="athleteStats">
                    <span>{(s.total_distance_m / 1000).toFixed(1)} km total</span>
                    <span>{(s.avg_distance_m / 1000).toFixed(1)} km avg</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
