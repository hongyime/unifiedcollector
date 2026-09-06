// Single source of truth for the UnifiedCollector platform registry.
//
// Historically this array was copy-pasted between src/background.js and
// src/platforms.js (loaded standalone via <script> in tabs.html) because
// MV3 service workers cannot dynamically importScripts() at boot without
// risking "failed importScripts prevents every alarm/listener from
// registering" — a real incident that produced the inline copy in
// background.js. With esbuild bundling at build time (format='iife'),
// importing from this module inlines the registry into every dist bundle
// with no runtime module loader / no importScripts call — which is exactly
// what MV3 CSP requires and what the historical inline copy was avoiding.
//
// TypeScript pilot: this is the first file in extension/src/shared/ to
// carry static types (see docs/plans/extension-bundler.md step 13). The
// rest of shared/ follows in step 14; the four large entry files
// (content / background / inject / popup / tabs) follow in step 15.

/** Public shape of one entry in the platform registry. */
export interface Platform {
  /** Short slug used for keys, ingest routing, ban walls, etc. */
  readonly id: string;
  /** Human-readable label rendered in the popup and options page. */
  readonly label: string;
  /** Canonical URL opened by "Open all tabs". */
  readonly url: string;
  /** Canonical hostname used for cookie / tab matching. */
  readonly host: string;
  /**
   * Additional hostnames that resolve to the same platform (e.g. the
   * `twitter.com` legacy hostname for `x`). Used by tab-matching helpers.
   */
  readonly aliasHosts?: readonly string[];
  /** Origin used for `chrome.cookies.get()` lookups. */
  readonly cookieUrl: string;
  /** Cookie name whose presence indicates a logged-in session. */
  readonly cookie: string;
  /** `true` if a content-script scraper exists (see content.js PLATFORMS). */
  readonly scraper: boolean;
  /**
   * `true` if the platform is scrapeable without logging in (e.g. Lemon8
   * For-You). The launcher renders "no login needed" instead of a red
   * "not logged in" badge.
   */
  readonly noLogin?: boolean;
  /**
   * Extra URLs that "expanded tabs" mode opens for broader coverage
   * (e.g. Instagram DM inbox, TikTok /foryou + /explore).
   */
  readonly optionalExtraUrls?: readonly string[];
}

/**
 * `scraper: true` → a content-script scraper exists (see content.js
 * PLATFORMS registry). `cookie` is the auth cookie used to detect login;
 * `noLogin: true` → the platform is scrapeable without logging in
 * (e.g. Lemon8 For-You), so the launcher shows "no login needed" instead
 * of a red "not logged in" badge.
 */
export const UC_PLATFORMS: readonly Platform[] = [
  { id: "instagram", label: "Instagram",   url: "https://www.instagram.com/",       host: "www.instagram.com",  cookieUrl: "https://www.instagram.com",      cookie: "sessionid",  scraper: true, optionalExtraUrls: ["https://www.instagram.com/direct/inbox/"] },
  // Threads moved threads.net → threads.com in Apr 2025 (.net just redirects).
  { id: "threads",   label: "Threads",     url: "https://www.threads.com/",         host: "www.threads.com",    cookieUrl: "https://www.threads.com",        cookie: "sessionid",  scraper: true },
  // Optional expanded coverage: /foryou and /explore add broader discovery.
  // Keep one visible /following tab by default to prioritize subscribed feeds
  // and avoid browser memory spikes.
  { id: "tiktok",    label: "TikTok",      url: "https://www.tiktok.com/following", host: "www.tiktok.com",     cookieUrl: "https://www.tiktok.com",         cookie: "sessionid",  scraper: true, optionalExtraUrls: ["https://www.tiktok.com/foryou", "https://www.tiktok.com/explore"] },
  // Lemon8's SPA renders "Not found" for /feed/<cat> and legacy paths as of
  // 2026-08-05. Keep one visible topic tab only; the headless Lemon8 collector
  // handles broader coverage without pinning extra Chrome tabs.
  { id: "lemon8",    label: "Lemon8",      url: "https://www.lemon8-app.com/topic/singapore?region=sg", host: "www.lemon8-app.com", cookieUrl: "https://www.lemon8-app.com",     cookie: "sessionid",  scraper: false, noLogin: true },
  { id: "x",         label: "Twitter / X", url: "https://x.com/home",               host: "x.com",              aliasHosts: ["twitter.com"], cookieUrl: "https://x.com",                  cookie: "auth_token", scraper: true },
  { id: "facebook",  label: "Facebook",    url: "https://www.facebook.com/",        host: "www.facebook.com",   cookieUrl: "https://www.facebook.com",       cookie: "c_user",     scraper: true },
  { id: "strava",    label: "Strava",      url: "https://www.strava.com/dashboard", host: "www.strava.com",     cookieUrl: "https://www.strava.com",         cookie: "_strava4_session", scraper: true },
];
