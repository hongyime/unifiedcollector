import { Suspense, lazy, startTransition, useEffect, useMemo, useState } from "react";
import AthleteRoster from "./components/AthleteRoster";
import FilterBar from "./components/FilterBar";
import { FilterProvider } from "./context/FilterContext";
import useActivities from "./hooks/useActivities";
import useAthleteRoutes from "./hooks/useAthleteRoutes";
import useAthletes from "./hooks/useAthletes";
import usePlayback from "./hooks/usePlayback";
import { fetchJson } from "./lib/api";
import { getActivityTypeColor, withAlpha } from "./lib/colors";

const MapCanvas = lazy(() => import("./components/MapCanvas"));
const HeatmapTab = lazy(() => import("./components/HeatmapTab"));
const RoutesTab = lazy(() => import("./components/RoutesTab"));
const AthletesTab = lazy(() => import("./components/AthletesTab"));
const NetworkTab = lazy(() => import("./components/NetworkTab"));

const TABS = [
  { id: "heatmap", label: "Heatmap" },
  { id: "routes", label: "Routes" },
  { id: "playback", label: "Playback" },
  { id: "athletes", label: "Athletes" },
  { id: "network", label: "Network" },
];

const DATE_SORT_OPTIONS = [
  { value: "activity", label: "Most activities" },
  { value: "name", label: "Name A-Z" },
  { value: "start", label: "First activity" }
];

const ATHLETE_ROUTE_SORT_OPTIONS = [
  { value: "recent", label: "Most recent first" },
  { value: "type", label: "Group by type" }
];

export default function App() {
  return (
    <FilterProvider>
      <AppInner />
    </FilterProvider>
  );
}

function AppInner() {
  const [activeTab, setActiveTab] = useState("playback");
  const [availableDates, setAvailableDates] = useState([]);
  const [selectedDate, setSelectedDate] = useState("");
  const [selectedAthleteId, setSelectedAthleteId] = useState(null);
  const [rosterFilter, setRosterFilter] = useState("all");
  const [viewerMode, setViewerMode] = useState("date");
  const [trailMode, setTrailMode] = useState("persist");
  const [speed, setSpeed] = useState(60);
  const [dateSort, setDateSort] = useState("activity");
  const [athleteRouteSort, setAthleteRouteSort] = useState("recent");
  const [athleteTypeFilter, setAthleteTypeFilter] = useState("all");

  const { data, loading, error, loadDate } = useActivities();
  const { athletes, loading: athletesLoading, error: athletesError, loadAthletes } = useAthletes();
  const {
    routes: athleteRoutes,
    loading: athleteRoutesLoading,
    error: athleteRoutesError,
    loadAthleteRoutes
  } = useAthleteRoutes();

  useEffect(() => {
    let cancelled = false;
    async function bootstrap() {
      const payload = await fetchJson("/api/v1/dates");
      if (cancelled) return;
      const dates = payload.dates ?? [];
      setAvailableDates(dates);
      if (dates.length) setSelectedDate(dates[0]);
    }
    bootstrap().catch((bootstrapError) => {
      if (!cancelled) { setAvailableDates([]); console.error(bootstrapError); }
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (viewerMode === "date") {
      if (!selectedDate) return;
      startTransition(() => { loadDate(selectedDate); loadAthletes(selectedDate, null); });
      return;
    }
    startTransition(() => { loadAthletes(null, null); });
  }, [selectedDate, viewerMode, loadAthletes, loadDate]);

  useEffect(() => {
    if (!athletes.length) { setSelectedAthleteId(null); return; }
    const exists = athletes.some((athlete) => athlete.athlete_id === selectedAthleteId);
    if (!exists && viewerMode === "athlete") setSelectedAthleteId(athletes[0].athlete_id);
    if (!exists && viewerMode === "date") setSelectedAthleteId(null);
  }, [athletes, selectedAthleteId, viewerMode]);

  useEffect(() => {
    if (viewerMode !== "athlete") return;
    loadAthleteRoutes(selectedAthleteId);
  }, [viewerMode, selectedAthleteId, loadAthleteRoutes]);

  useEffect(() => {
    setAthleteTypeFilter("all");
    setAthleteRouteSort("recent");
  }, [selectedAthleteId]);

  const selectedAthlete = useMemo(
    () => athletes.find((athlete) => athlete.athlete_id === selectedAthleteId) ?? null,
    [athletes, selectedAthleteId]
  );

  const rosterAthletes = useMemo(() => {
    const athleteFirstStarts = new Map();
    for (const trip of data?.trips ?? []) {
      if (trip.start_unix == null) continue;
      const current = athleteFirstStarts.get(trip.athlete_id);
      if (current == null || trip.start_unix < current) athleteFirstStarts.set(trip.athlete_id, trip.start_unix);
    }
    const copy = [...athletes];
    if (viewerMode === "date") {
      copy.sort((l, r) => compareDateAthletes(l, r, dateSort, athleteFirstStarts));
      return copy;
    }
    copy.sort(
      (l, r) =>
        Number(r.is_following) - Number(l.is_following) ||
        (r.activity_count || 0) - (l.activity_count || 0) ||
        l.name.localeCompare(r.name)
    );
    return copy;
  }, [athletes, viewerMode, dateSort, data]);

  const athleteTypeOptions = useMemo(() => {
    const values = new Set();
    for (const route of athleteRoutes?.routes ?? []) {
      if (route.sport_type) values.add(route.sport_type);
    }
    return ["all", ...Array.from(values).sort((l, r) => l.localeCompare(r))];
  }, [athleteRoutes]);

  const athleteModeRoutes = useMemo(() => {
    const routes = [...(athleteRoutes?.routes ?? [])];
    const filtered = athleteTypeFilter === "all" ? routes : routes.filter((r) => r.sport_type === athleteTypeFilter);
    filtered.sort((l, r) => {
      if (athleteRouteSort === "type") return l.sport_type.localeCompare(r.sport_type) || (r.start_unix || 0) - (l.start_unix || 0);
      return (r.start_unix || 0) - (l.start_unix || 0);
    });
    return filtered;
  }, [athleteRoutes, athleteTypeFilter, athleteRouteSort]);

  const athletePlayback = useMemo(() => {
    if (!selectedAthlete || !athleteModeRoutes.length) return null;
    const startCandidates = athleteModeRoutes.map((r) => r.start_unix).filter((v) => v != null);
    const endCandidates = athleteModeRoutes.map((r) => r.end_unix).filter((v) => v != null);
    if (!startCandidates.length || !endCandidates.length) return null;
    return {
      date: selectedAthlete.name,
      timezone: "Asia/Singapore",
      day_start_unix: Math.min(...startCandidates),
      day_end_unix: Math.max(...endCandidates),
      athlete_count: 1,
      trips: athleteModeRoutes.map((route) => ({
        activity_id: route.activity_id,
        athlete_id: selectedAthlete.athlete_id,
        athlete_name: selectedAthlete.name,
        activity_name: route.activity_name,
        sport_type: route.sport_type,
        start_unix: route.start_unix,
        end_unix: route.end_unix,
        privacy_zone_start: false,
        privacy_zone_end: false,
        truncation_point_start: null,
        truncation_point_end: null,
        stream_status: route.stream_status,
        calendar_date: route.calendar_date,
        color: route.color,
        path: route.path
      }))
    };
  }, [selectedAthlete, athleteModeRoutes]);

  const displayPlayback = viewerMode === "date" ? data : athletePlayback;
  const playback = usePlayback(displayPlayback, speed);
  const timelineDensity = useMemo(() => buildTimelineDensity(displayPlayback), [displayPlayback]);
  const hasStoredDate = selectedDate && availableDates.includes(selectedDate);

  useEffect(() => {
    if (!displayPlayback?.trips?.length) return;
    const candidates = viewerMode === "date" && selectedAthleteId
      ? displayPlayback.trips.filter((t) => t.athlete_id === selectedAthleteId && t.start_unix != null)
      : displayPlayback.trips.filter((t) => t.start_unix != null);
    const sorted = [...candidates].sort((l, r) => (l.start_unix ?? 0) - (r.start_unix ?? 0));
    if (sorted.length && sorted[0].start_unix != null) playback.setCurrentTime(sorted[0].start_unix);
  }, [viewerMode, selectedAthleteId, displayPlayback, playback.setCurrentTime]);

  return (
    <div className="shell">
      {/* Tab bar */}
      <nav className="tabBar" role="tablist" aria-label="View">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            className={`tabBtn ${activeTab === tab.id ? "tabBtnActive" : ""}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {/* Filter bar — shared across all tabs */}
      <FilterBar />

      {/* Tab panels */}
      <Suspense fallback={<div className="tabLoading">Loading...</div>}>
        {activeTab === "heatmap" && <HeatmapTab />}
        {activeTab === "routes" && <RoutesTab />}
        {activeTab === "athletes" && <AthletesTab />}
        {activeTab === "network" && <NetworkTab />}
        {activeTab === "playback" && (
          <div className="playbackLayout">
            <MapCanvas
              mapMode={viewerMode}
              playback={displayPlayback}
              currentTime={playback.currentTime}
              trailMode={trailMode}
              loading={viewerMode === "date" ? loading : athleteRoutesLoading}
              error={viewerMode === "date" ? error : athleteRoutesError}
              isPlaying={playback.isPlaying}
              dayStart={displayPlayback?.day_start_unix}
              dayEnd={displayPlayback?.day_end_unix}
              onPlayToggle={playback.togglePlaying}
              onReset={playback.reset}
              onScrub={playback.setCurrentTime}
              speed={speed}
              onSpeedChange={setSpeed}
              onTrailModeChange={setTrailMode}
              timelineDensity={timelineDensity}
              selectedAthlete={viewerMode === "athlete" ? selectedAthlete : null}
              highlightedAthleteId={viewerMode === "date" ? selectedAthleteId : null}
            />

            <section className="controlsSection">
              <div className="controlsRow controlsRowPrimary">
                <section className="panel controlCard controlCardView">
                  <p className="eyebrow">Viewer mode</p>
                  <div className="modeToggle" role="tablist" aria-label="Viewer mode">
                    <button type="button" className={`modeButton ${viewerMode === "date" ? "selected" : ""}`} onClick={() => setViewerMode("date")}>Date playback</button>
                    <button type="button" className={`modeButton ${viewerMode === "athlete" ? "selected" : ""}`} onClick={() => setViewerMode("athlete")}>Athlete view</button>
                  </div>
                </section>

                {viewerMode === "date" ? (
                  <section className="panel controlCard controlCardFilters">
                    <div className="panelHeader">
                      <h2>Date playback</h2>
                      <button type="button" className={`clearSelectionButton ${selectedAthleteId == null ? "selected" : ""}`} onClick={() => setSelectedAthleteId(null)}>All athletes</button>
                    </div>
                    <div className="controlGrid">
                      <label className="field">
                        <span>Date</span>
                        <input type="date" value={selectedDate} onChange={(e) => setSelectedDate(e.target.value)} disabled={!availableDates.length} />
                      </label>
                      <label className="field">
                        <span>Sort athletes</span>
                        <select value={dateSort} onChange={(e) => setDateSort(e.target.value)}>
                          {DATE_SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                      </label>
                    </div>
                    <p className={`microStatus ${hasStoredDate ? "isStored" : "isEmpty"}`}>
                      {hasStoredDate ? "Saved data ready." : "No saved routes for this date yet."}
                    </p>
                  </section>
                ) : (
                  <section className="panel controlCard controlCardFilters">
                    <div className="panelHeader">
                      <div>
                        <h2>Athlete view</h2>
                        <span className="selectedSummary">{selectedAthlete ? selectedAthlete.name : "Choose an athlete"}</span>
                      </div>
                      <div className="typeChips typeChipsHeader" role="tablist" aria-label="Activity type filter">
                        {athleteTypeOptions.map((type) => (
                          <button key={type} type="button" className={`typeChip ${athleteTypeFilter === type ? "selected" : ""}`} onClick={() => setAthleteTypeFilter(type)} style={buildTypeChipStyle(type, athleteTypeFilter === type)}>
                            {type === "all" ? "All activity types" : type}
                          </button>
                        ))}
                      </div>
                    </div>
                    <div className="controlGrid">
                      <label className="field">
                        <span>Sort routes</span>
                        <select value={athleteRouteSort} onChange={(e) => setAthleteRouteSort(e.target.value)}>
                          {ATHLETE_ROUTE_SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                      </label>
                    </div>
                  </section>
                )}
              </div>

              <div className="controlsRow controlsRowRoster">
                <AthleteRoster
                  title={viewerMode === "date" ? "Athletes on this date" : "Choose an athlete"}
                  athletes={rosterAthletes}
                  loading={athletesLoading}
                  error={athletesError}
                  selectedAthleteId={selectedAthleteId}
                  filter={rosterFilter}
                  onFilterChange={setRosterFilter}
                  onSelectAthlete={setSelectedAthleteId}
                  activityLabel={viewerMode === "date" ? "activities" : "saved routes"}
                />
              </div>
            </section>
          </div>
        )}
      </Suspense>
    </div>
  );
}

function buildTimelineDensity(playback) {
  if (!playback?.trips?.length || playback.day_start_unix == null || playback.day_end_unix == null) return [];
  const bucketCount = 72;
  const range = Math.max(1, playback.day_end_unix - playback.day_start_unix);
  const buckets = Array.from({ length: bucketCount }, () => 0);
  for (const trip of playback.trips) {
    const start = Math.max(playback.day_start_unix, trip.start_unix ?? playback.day_start_unix);
    const end = Math.min(playback.day_end_unix, trip.end_unix ?? start);
    const startIndex = Math.max(0, Math.floor(((start - playback.day_start_unix) / range) * bucketCount));
    const endIndex = Math.min(bucketCount - 1, Math.floor(((Math.max(start, end) - playback.day_start_unix) / range) * bucketCount));
    for (let i = startIndex; i <= endIndex; i++) buckets[i] += 1;
  }
  return buckets;
}

function compareDateAthletes(l, r, sortMode, firstStarts) {
  if (sortMode === "name") return l.name.localeCompare(r.name);
  if (sortMode === "start") {
    const ls = firstStarts.get(l.athlete_id) ?? Number.MAX_SAFE_INTEGER;
    const rs = firstStarts.get(r.athlete_id) ?? Number.MAX_SAFE_INTEGER;
    return ls - rs || l.name.localeCompare(r.name);
  }
  return (r.activity_count || 0) - (l.activity_count || 0) || l.name.localeCompare(r.name);
}

function buildTypeChipStyle(type, selected) {
  const color = getActivityTypeColor(type);
  return {
    borderColor: selected ? color : withAlpha(color, 0.42),
    color,
    background: selected ? withAlpha(color, 0.16) : withAlpha(color, 0.08)
  };
}
