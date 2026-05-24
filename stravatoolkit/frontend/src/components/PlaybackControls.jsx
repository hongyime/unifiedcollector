const SPEED_OPTIONS = [
  { label: "0.25x", value: 15 },
  { label: "1x", value: 60 },
  { label: "4x", value: 240 },
  { label: "12x", value: 720 }
];

export default function PlaybackControls({
  currentTime,
  dayStart,
  dayEnd,
  isPlaying,
  onPlayToggle,
  onReset,
  onScrub,
  speed,
  onSpeedChange,
  trailMode,
  onTrailModeChange,
  timelineDensity
}) {
  const disabled = dayStart == null || dayEnd == null;
  const maxDensity = Math.max(...timelineDensity, 1);
  const showDate = dayStart != null && dayEnd != null && dayEnd - dayStart > 86400;

  return (
    <section className="playbackDock">
      <div className="playbackDockTop">
        <div className="playbackClockGroup">
          <strong className="playbackClock">{formatTimelineValue(currentTime ?? dayStart, showDate)}</strong>
        </div>

        <div className="playbackQuickControls">
          <button type="button" onClick={onPlayToggle} disabled={disabled}>
            {isPlaying ? "Pause" : "Play"}
          </button>
          <button type="button" onClick={onReset} disabled={disabled}>
            Reset
          </button>
          <label className="compactField compactFieldInline">
            <span>Speed</span>
            <select value={speed} onChange={(event) => onSpeedChange(Number(event.target.value))}>
              {SPEED_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="compactField compactFieldInline compactFieldTrail">
            <span>Trail</span>
            <select value={trailMode} onChange={(event) => onTrailModeChange(event.target.value)}>
              <option value="persist">Full routes</option>
              <option value="off">Endpoints only</option>
            </select>
          </label>
        </div>
      </div>

      <div className="timelineDock">
        <div className="timelineTrack">
          <div className="timelineHeatmap" aria-hidden="true">
            {(timelineDensity.length ? timelineDensity : [0]).map((count, index) => (
              <span
                key={`${index}-${count}`}
                className="timelineSegment"
                style={{ backgroundColor: colorForIntensity(maxDensity ? count / maxDensity : 0) }}
              />
            ))}
          </div>
          <input
            className="timelineSlider"
            type="range"
            min={dayStart ?? 0}
            max={dayEnd ?? 1}
            value={currentTime ?? dayStart ?? 0}
            onChange={(event) => onScrub(Number(event.target.value))}
            disabled={disabled}
            aria-label="Timeline"
          />
        </div>
      </div>
    </section>
  );
}

function colorForIntensity(intensity) {
  if (intensity <= 0) {
    return "rgba(53, 61, 71, 0.9)";
  }
  if (intensity < 0.25) {
    return "rgba(252, 82, 0, 0.28)";
  }
  if (intensity < 0.5) {
    return "rgba(252, 82, 0, 0.42)";
  }
  if (intensity < 0.8) {
    return "rgba(252, 82, 0, 0.62)";
  }
  return "rgba(252, 82, 0, 0.88)";
}

function formatTimelineValue(unixSeconds, showDate) {
  if (!unixSeconds) {
    return "--:--:--";
  }

  const options = showDate
    ? {
        hour12: false,
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZone: "Asia/Singapore"
      }
    : {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZone: "Asia/Singapore"
      };

  return new Date(unixSeconds * 1000).toLocaleString("en-SG", options);
}
