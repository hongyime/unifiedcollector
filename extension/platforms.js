// Shared social-platform registry for UnifiedCollector Bridge.
// Loaded by the service worker (importScripts) and the launcher/popup pages.
// `scraper: true` -> a content-script scraper exists (content.js PLATFORMS).
// `cookie` is the auth cookie used to detect login; `noLogin: true` -> the
// platform is scrapeable without logging in (e.g. Lemon8 For-You), so the
// launcher shows "no login needed" instead of a red "not logged in".
globalThis.UC_PLATFORMS = [
  { id: "instagram", label: "Instagram",   url: "https://www.instagram.com/",   host: "www.instagram.com",   cookieUrl: "https://www.instagram.com",   cookie: "sessionid",  scraper: true, extraUrls: ["https://www.instagram.com/direct/inbox/"] },
  // Threads moved threads.net -> threads.com in Apr 2025 (.net just redirects).
  { id: "threads",   label: "Threads",     url: "https://www.threads.com/",     host: "www.threads.com",     cookieUrl: "https://www.threads.com",     cookie: "sessionid",  scraper: true  },
  { id: "tiktok",    label: "TikTok",      url: "https://www.tiktok.com/following", host: "www.tiktok.com",  cookieUrl: "https://www.tiktok.com",      cookie: "sessionid",  scraper: true, extraUrls: ["https://www.tiktok.com/foryou"] },
  { id: "lemon8",    label: "Lemon8",      url: "https://www.lemon8-app.com/",  host: "www.lemon8-app.com",  cookieUrl: "https://www.lemon8-app.com",  cookie: "sessionid",  scraper: true, noLogin: true },
  { id: "x",         label: "Twitter / X", url: "https://x.com/home",           host: "x.com",               aliasHosts: ["twitter.com"], cookieUrl: "https://x.com",               cookie: "auth_token", scraper: true  },
  { id: "facebook",  label: "Facebook",    url: "https://www.facebook.com/",    host: "www.facebook.com",    cookieUrl: "https://www.facebook.com",    cookie: "c_user",     scraper: true  },
  { id: "strava",    label: "Strava",      url: "https://www.strava.com/dashboard", host: "www.strava.com",  cookieUrl: "https://www.strava.com",      cookie: "_strava4_session", scraper: true },
];

// Also expose as a normal const for <script>-included pages.
if (typeof window !== "undefined") window.UC_PLATFORMS = globalThis.UC_PLATFORMS;
