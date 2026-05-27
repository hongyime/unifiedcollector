function formatBackfillStatus(status) {
  if (!status) {
    return "Pending";
  }
  if (status === "degraded") {
    return "Retry pending";
  }
  return status
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function formatTimestamp(value) {
  if (!value) {
    return null;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

export default function AthleteDetailPanel({ detail, loading, error }) {
  const isDegraded = detail?.backfill_status === "degraded";

  return (
    <section className="panel athleteDetailPanel">
      <div className="rosterHeader">
        <h2>Athlete Detail</h2>
        {detail ? (
          <span style={{ color: `rgb(${detail.color.join(" ")})` }}>{detail.activity_count}</span>
        ) : null}
      </div>
      {loading ? <p>Loading athlete detail...</p> : null}
      {error ? <p className="errorText">{error}</p> : null}
      {!loading && !error && !detail ? <p>Select an athlete to inspect backfill progress.</p> : null}
      {detail ? (
        <div className="athleteDetailBody">
          <p className="eyebrow">Backfill Status</p>
          <h3>{detail.name}</h3>
          <p className={`backfillStatusValue ${isDegraded ? "isDegraded" : ""}`}>
            {formatBackfillStatus(detail.backfill_status)}
          </p>
          {isDegraded ? (
            <div className="issueCard">
              <strong>{detail.backfill_last_issue_message ?? "Profile page loaded, but no activities could be parsed."}</strong>
              <span className="issueCardMeta">
                Issue code: {detail.backfill_last_issue_code ?? "unknown"}
              </span>
              {detail.backfill_last_issue_at ? (
                <span className="issueCardMeta">
                  Last seen: {formatTimestamp(detail.backfill_last_issue_at)}
                </span>
              ) : null}
              <p className="detailNote">
                This athlete stays on the same month cursor and will be retried on future backfill runs.
              </p>
            </div>
          ) : null}
          <div className="detailGrid">
            <div className="metricCard">
              <span>Oldest seen</span>
              <strong>{detail.backfill_oldest_seen_utc ?? "None yet"}</strong>
            </div>
            <div className="metricCard">
              <span>Completed</span>
              <strong>{detail.backfill_completed_at ?? "In progress"}</strong>
            </div>
          </div>
          <div className="recentList">
            <p className="eyebrow">Recent Activities</p>
            {detail.recent_activities.length ? (
              detail.recent_activities.map((activity) => (
                <article key={activity.activity_id} className="recentRow">
                  <strong>{activity.activity_name || "Unnamed activity"}</strong>
                  <span>
                    {activity.calendar_date} · {activity.sport_type} · {activity.stream_status}
                  </span>
                </article>
              ))
            ) : (
              <p>No ingested activities for this athlete yet.</p>
            )}
          </div>
        </div>
      ) : null}
    </section>
  );
}
