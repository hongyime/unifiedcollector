export const API_BASE = import.meta.env.VITE_API_URL || "";
export const WS_BASE = import.meta.env.VITE_WS_URL || `ws://${window.location.host}`;

export const SOURCES = [
  "github",
  "website",
  "instagram",
  "telegram",
  "tiktok",
  "youtube",
  "lemon8",
  "strava",
  "whatsapp",
  "search",
  "beeper",
  "facebook",
  "threads",
  "x",
] as const;

export type SourceName = (typeof SOURCES)[number];
