export default function CoveragePanel({ coverage, loading, error, selectedMonth, onSelectMonth }) {
  return (
    <section className="panel coveragePanel">
      <div className="rosterHeader">
        <h2>Archive Coverage</h2>
        <span>{selectedMonth || `${coverage?.month_count ?? 0} months`}</span>
      </div>
      {loading ? <p>Loading archive coverage...</p> : null}
      {error ? <p className="errorText">{error}</p> : null}
      {!loading && !error && !coverage ? <p>No coverage summary yet.</p> : null}
      {coverage ? (
        <>
          <div className="detailGrid">
            <div className="metricCard">
              <span>Activities</span>
              <strong>{coverage.activity_count}</strong>
            </div>
            <div className="metricCard">
              <span>Ready streams</span>
              <strong>{coverage.ready_count}</strong>
            </div>
          </div>
          <div className="coverageYears">
            {coverage.years.map((year) => (
              <section key={year.year} className="coverageYear">
                <div className="coverageYearHeader">
                  <strong>{year.year}</strong>
                  <span>
                    {year.activity_count} activities · up to {year.athlete_count} athletes
                  </span>
                </div>
                <div className="coverageMonths">
                  {year.months.map((month) => (
                    <button
                      key={month.month}
                      type="button"
                      className={`coverageMonth ${selectedMonth === month.month ? "selected" : ""}`}
                      onClick={() => onSelectMonth?.(selectedMonth === month.month ? "" : month.month)}
                    >
                      <strong>{month.month}</strong>
                      <span>
                        {month.activity_count} acts · {month.athlete_count} athletes · {month.ready_count} ready
                      </span>
                    </button>
                  ))}
                </div>
              </section>
            ))}
          </div>
        </>
      ) : null}
    </section>
  );
}
