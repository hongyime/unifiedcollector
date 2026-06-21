// Shared social-platform registry for UnifiedCollector Bridge.
// Loaded by the service worker (importScripts) and the launcher/popup pages.
// `scraper: true` means a content-script scraper exists (content.js PLATFORMS);
// the rest are "login-ready" — opening/logging in primes them so scraping works
// the moment a scraper is added. `cookie` is the auth cookie used to detect login.
globalThis.UC_PLATFORMS = [
  { id: "instagram", label: "Instagram",   url: "https://www.instagram.com/",     host: "www.instagram.com",   cookieUrl: "https://www.instagram.com",   cookie: "sessionid",  scraper: true  },
  { id: "threads",   label: "Threads",     url: "https://www.threads.net/",       host: "www.threads.net",     cookieUrl: "https://www.threads.net",     cookie: "sessionid",  scraper: false },
  { id: "tiktok",    label: "TikTok",      url: "https://www.tiktok.com/",        host: "www.tiktok.com",      cookieUrl: "https://www.tiktok.com",      cookie: "sessionid",  scraper: false },
  { id: "lemon8",    label: "Lemon8",      url: "https://www.lemon8-app.com/",    host: "www.lemon8-app.com",  cookieUrl: "https://www.lemon8-app.com",  cookie: "sessionid",  scraper: false },
  { id: "x",         label: "Twitter / X", url: "https://x.com/home",             host: "x.com",               cookieUrl: "https://x.com",               cookie: "auth_token", scraper: false },
  { id: "facebook",  label: "Facebook",    url: "https://www.facebook.com/",      host: "www.facebook.com",    cookieUrl: "https://www.facebook.com",    cookie: "c_user",     scraper: false },
];

// Also expose as a normal const for <script>-included pages.
if (typeof window !== "undefined") window.UC_PLATFORMS = globalThis.UC_PLATFORMS;
