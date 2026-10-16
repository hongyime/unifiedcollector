"use strict";
(() => {
  // src/shared/platforms.ts
  var UC_PLATFORMS = [
    { id: "instagram", label: "Instagram", url: "https://www.instagram.com/", host: "www.instagram.com", cookieUrl: "https://www.instagram.com", cookie: "sessionid", scraper: true },
    // Threads moved threads.net → threads.com in Apr 2025 (.net just redirects).
    { id: "threads", label: "Threads", url: "https://www.threads.com/", host: "www.threads.com", cookieUrl: "https://www.threads.com", cookie: "sessionid", scraper: true },
    // Keep one visible /following tab per platform; broader coverage runs in that tab.
    { id: "tiktok", label: "TikTok", url: "https://www.tiktok.com/following", host: "www.tiktok.com", cookieUrl: "https://www.tiktok.com", cookie: "sessionid", scraper: true },
    // Keep the single visible tab on the populated For You feed; the extension
    // supplements the headless API collector with paced DOM media candidates.
    { id: "lemon8", label: "Lemon8", url: "https://www.lemon8-app.com/feed/foryou", host: "www.lemon8-app.com", cookieUrl: "https://www.lemon8-app.com", cookie: "sessionid", scraper: true, noLogin: true },
    { id: "x", label: "Twitter / X", url: "https://x.com/home", host: "x.com", aliasHosts: ["twitter.com"], cookieUrl: "https://x.com", cookie: "auth_token", scraper: true },
    { id: "facebook", label: "Facebook", url: "https://www.facebook.com/", host: "www.facebook.com", cookieUrl: "https://www.facebook.com", cookie: "c_user", scraper: true },
    { id: "strava", label: "Strava", url: "https://www.strava.com/dashboard", host: "www.strava.com", cookieUrl: "https://www.strava.com", cookie: "_strava4_session", scraper: true },
    { id: "pinterest", label: "Pinterest", url: "https://www.pinterest.com/", host: "pinterest.com", cookieUrl: "https://www.pinterest.com", cookie: "_pinterest_sess", scraper: true },
    { id: "snapchat", label: "Snapchat", url: "https://www.snapchat.com/", host: "snapchat.com", cookieUrl: "https://www.snapchat.com", cookie: "", scraper: true, noLogin: true },
    { id: "airbnb", label: "Airbnb", url: "https://www.airbnb.com/", host: "airbnb.com", aliasHosts: ["airbnb.com.sg", "airbnb.co.uk", "airbnb.co.kr"], cookieUrl: "https://www.airbnb.com", cookie: "", scraper: true, noLogin: true }
  ];

  // src/platforms.js
  globalThis.UC_PLATFORMS = UC_PLATFORMS;
  if (typeof window !== "undefined") window.UC_PLATFORMS = UC_PLATFORMS;
})();
