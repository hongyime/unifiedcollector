import { useMemo, useState } from "react";

const FILTERS = [
  { id: "all", label: "All tracked" },
  { id: "following", label: "Following" },
  { id: "tracked", label: "Tracked only" }
];

export default function AthleteRoster({
  title,
  athletes,
  loading,
  error,
  selectedAthleteId,
  filter = "all",
  onFilterChange,
  onSelectAthlete,
  activityLabel = "activities"
}) {
  const [searchTerm, setSearchTerm] = useState("");

  const visibleAthletes = useMemo(() => {
    const term = searchTerm.trim().toLowerCase();
    return athletes.filter((athlete) => {
      if (filter === "following" && !athlete.is_following) {
        return false;
      }
      if (filter === "tracked" && athlete.is_following) {
        return false;
      }
      if (!term) {
        return true;
      }
      return athlete.name.toLowerCase().includes(term);
    });
  }, [athletes, filter, searchTerm]);

  return (
    <section className="panel rosterPanel">
      <div className="rosterHeader">
        <div>
          <p className="eyebrow">Athletes</p>
          <h2>{title}</h2>
        </div>
        <span className="rosterCount">{visibleAthletes.length}</span>
      </div>

      <div className="rosterToolbar">
        <div className="filterChips">
          {FILTERS.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`filterChip ${filter === option.id ? "selected" : ""}`}
              onClick={() => onFilterChange?.(option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>

        <label className="field rosterSearch">
          <span>Search</span>
          <input
            type="search"
            placeholder="Find an athlete"
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
          />
        </label>
      </div>

      {loading ? <p className="emptyState">Loading athletes...</p> : null}
      {error ? <p className="errorText">{error}</p> : null}

      {!loading && !error ? (
        <div className="rosterGrid">
          {visibleAthletes.length ? (
            visibleAthletes.map((athlete) => (
              <button
                key={athlete.athlete_id}
                type="button"
                className={`rosterCard ${selectedAthleteId === athlete.athlete_id ? "selected" : ""}`}
                onClick={() => onSelectAthlete?.(athlete.athlete_id)}
              >
                <div className="rosterCardTop">
                  <strong>{athlete.name}</strong>
                  <span className={`statusTag ${athlete.is_following ? "isFollowing" : "isTrackedOnly"}`}>
                    {athlete.is_following ? "Following" : "Tracked only"}
                  </span>
                </div>
                <span className="rosterCardMeta">
                  {athlete.activity_count} {activityLabel}
                </span>
              </button>
            ))
          ) : (
            <p className="emptyState">No athletes match this filter.</p>
          )}
        </div>
      ) : null}
    </section>
  );
}
