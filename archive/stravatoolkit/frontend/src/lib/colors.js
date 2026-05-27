export const STRAVA_ORANGE = "#FC5200";
export const MUTED_ROUTE = "#78808A";

export const DATE_PALETTE = [
  "#FC5200",
  "#5289FF",
  "#48C499",
  "#DA9949",
  "#B878FF",
  "#F07082",
  "#52BACC",
  "#A0B160"
];

export const ACTIVITY_TYPE_COLORS = {
  Run: "#FC5200",
  Ride: "#5289FF",
  Walk: "#48C499",
  Hike: "#76BF6C",
  TrailRun: "#D7793A",
  Workout: "#B878FF",
  Swim: "#4EC1DE",
  Rowing: "#5CA0E2"
};

export function getActivityTypeColor(type) {
  if (!type || type === "all") {
    return STRAVA_ORANGE;
  }
  return ACTIVITY_TYPE_COLORS[type] ?? "#D6A15C";
}

export function withAlpha(hexColor, alpha) {
  const normalized = hexColor.replace("#", "");
  const full = normalized.length === 3
    ? normalized.split("").map((value) => value + value).join("")
    : normalized;
  const red = Number.parseInt(full.slice(0, 2), 16);
  const green = Number.parseInt(full.slice(2, 4), 16);
  const blue = Number.parseInt(full.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}
