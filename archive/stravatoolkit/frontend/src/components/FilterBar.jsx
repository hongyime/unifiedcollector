import { useFilters } from "../context/FilterContext";

const SPORT_TYPES = ["", "Run", "Ride", "Walk", "Hike", "Swim", "Workout"];

export default function FilterBar() {
  const { filters, updateFilter, clearFilters } = useFilters();

  return (
    <div className="filterBar">
      <label className="filterField">
        <span>Sport</span>
        <select value={filters.sportType} onChange={(e) => updateFilter("sportType", e.target.value)}>
          {SPORT_TYPES.map((t) => (
            <option key={t} value={t}>{t || "All"}</option>
          ))}
        </select>
      </label>

      <label className="filterField">
        <span>From</span>
        <input type="date" value={filters.dateFrom} onChange={(e) => updateFilter("dateFrom", e.target.value)} />
      </label>

      <label className="filterField">
        <span>To</span>
        <input type="date" value={filters.dateTo} onChange={(e) => updateFilter("dateTo", e.target.value)} />
      </label>

      <button className="filterClear" onClick={clearFilters} type="button">
        Clear
      </button>
    </div>
  );
}
