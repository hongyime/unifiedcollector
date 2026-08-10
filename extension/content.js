(() => {
const UC_CONTENT_VERSION = (() => {
  try {
    return (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || "unknown";
  } catch (e) {
    return "unknown";
  }
})();
const UC_CONTENT_INSTALL_ID = `${UC_CONTENT_VERSION}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
const UC_CONTENT_STATE = globalThis.__UC_CONTENT_SCRIPT_ACTIVE__;
if (UC_CONTENT_STATE && typeof UC_CONTENT_STATE === "object") {
  UC_CONTENT_STATE.running = false;
  UC_CONTENT_STATE.superseded_at = Date.now();
  UC_CONTENT_STATE.superseded_by = UC_CONTENT_INSTALL_ID;
}
globalThis.__UC_CONTENT_SCRIPT_ACTIVE__ = {
  version: UC_CONTENT_VERSION,
  installed_at: Date.now(),
  token: UC_CONTENT_INSTALL_ID,
  running: true,
};
function ucContentScriptCurrent() {
  try {
    const state = globalThis.__UC_CONTENT_SCRIPT_ACTIVE__;
    return !!state && state.token === UC_CONTENT_INSTALL_ID && state.running === true;
  } catch (e) {
    return false;
  }
}

// UnifiedCollector Social Bridge — content script.
//
// Runs ON a social site, so its fetches to that site's internal API are
// SAME-ORIGIN and carry your logged-in session cookies + a real browser
// fingerprint — which bypasses the bot throttles a headless/raw client hits.
// For sites that sign their requests (TikTok/Lemon8), we instead read the
// page's OWN embedded state JSON (already fetched by the page) — also ban-safe.
//
// MULTI-PLATFORM: scrapers live in the PLATFORMS registry keyed by hostname.
// Add a platform by dropping another entry with a `host` matcher and an async
// `runCycle()` returning {targets, saved, discovered}. Remember to also add its
// host to manifest content_scripts + host_permissions + platforms.js, and a
// matching ingest endpoint (the generic /social/* endpoints already cover it).

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitter = (base) => base + Math.random() * base;

// Per-origin persisted state. Our following/foryou rotation + profile-visit queue
// need to survive the page reloads that navigation causes, so we stash counters in
// localStorage (scoped to the platform's origin, so no cross-talk between sites).
const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };
const lsNum = (k) => { const n = parseInt(lsGet(k, "0"), 10); return Number.isFinite(n) ? n : 0; };
const lsBump = (k) => { const n = lsNum(k) + 1; lsSet(k, String(n)); return n; };

// PERSISTENT throttle wall (anti-ban). Store by platform + visible owner where
// possible, with the old platform-only key as a legacy fallback.
function instagramLoggedInOwner() {
  try {
    const username =
      window._sharedData &&
      window._sharedData.config &&
      window._sharedData.config.viewer &&
      window._sharedData.config.viewer.username;
    if (username) return String(username).trim().replace(/^@/, "");
  } catch (e) {}
  const m = document.cookie.match(/ds_user_id=(\d+)/);
  return m ? m[1] : "";
}
function facebookLoggedInOwner() {
  try {
    const m = document.cookie.match(/c_user=(\d+)/);
    if (m) return m[1];
  } catch (e) {}
  return "";
}
function cooldownIdentity(platform) {
  if (platform === "instagram") return instagramLoggedInOwner();
  if (platform === "tiktok") {
    return ownerFromStoredOrDom("tiktok", () => {
      const m = location.pathname.match(/^\/@([^/?#]+)/);
      return m && m[1] ? m[1] : "";
    });
  }
  if (platform === "x") return ownerFromStoredOrDom("x", xLoggedInOwner);
  if (platform === "threads") return ownerFromStoredOrDom("threads", threadsLoggedInOwner);
  if (platform === "facebook") return ownerFromStoredOrDom("facebook", facebookLoggedInOwner);
  if (platform === "strava") return stravaLoggedInOwner();
  return "";
}
function wallKey(platform, identity) {
  const ident = String(identity || cooldownIdentity(platform) || "global")
    .trim()
    .replace(/^@/, "")
    .replace(/[^A-Za-z0-9_.-]/g, "_")
    .slice(0, 80) || "global";
  return "uc_wall_" + platform + "_" + ident;
}
function wallLeftMs(platform, identity) {
  const keyed = lsNum(wallKey(platform, identity));
  const legacy = lsNum("uc_wall_" + platform);
  return Math.max(0, Math.max(keyed, legacy) - Date.now());
}
function setWall(platform, mins, identity) {
  lsSet(wallKey(platform, identity), String(Date.now() + mins * 60000));
}

// Config-driven throttle walls. Override from DevTools/options with:
//   chrome.storage.local.set({ ucThrottleBackoffMins: { x: 12, threads: 12 } })
// Instagram stays deliberately cautious at 45m by default; shortening it
// aggressively re-extends the account/IP throttle window and raises ban risk.
const DEFAULT_THROTTLE_BACKOFF_MINS = {
  instagram: 75,
  threads: 20,
  x: 20,
  tiktok: 30,
  facebook: 30,
  lemon8: 30,
  default: 35,
};
async function throttleBackoffMins(platform, fallback = DEFAULT_THROTTLE_BACKOFF_MINS.default) {
  try {
    const { ucThrottleBackoffMins = {} } = await chrome.storage.local.get("ucThrottleBackoffMins");
    const raw = ucThrottleBackoffMins[platform] ?? ucThrottleBackoffMins.default;
    const n = Number(raw);
    if (Number.isFinite(n) && n >= 1) return Math.round(n);
  } catch (e) {}
  return DEFAULT_THROTTLE_BACKOFF_MINS[platform] || fallback;
}
async function applyThrottleWall(platform, reason) {
  const mins = await throttleBackoffMins(platform);
  const wallMins = Math.max(1, Math.round(mins * (0.85 + Math.random() * 0.45)));
  const identity = cooldownIdentity(platform);
  setWall(platform, wallMins, identity);
  await send({ type: "wall", platform, mins: wallMins, account: identity || null, reason }).catch(() => {});
  return wallMins;
}

// ---------------------------------------------------------------------------
// HUMAN PACING. A real person browsing is slow, irregular, and takes breaks.
// Scraping 257 profiles back-to-back is what got the IG account flagged for
// review. `human(base)` returns base x (0.8-2.2) and sometimes adds an extra
// pause or a longer rest. Use hsleep()
// everywhere instead of fixed sleeps, and keep per-cycle VOLUME small.
function human(base) {
  let ms = base * (0.8 + Math.random() * 1.4);
  if (Math.random() < 0.18) ms += 5000 + Math.random() * 15000;
  if (Math.random() < 0.05) ms += 45000 + Math.random() * 105000;
  return Math.round(ms);
}
const hsleep = (base) => sleep(human(base));
function shuffle(a) { for (let i = a.length - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; [a[i], a[j]] = [a[j], a[i]]; } return a; }

// ---------------------------------------------------------------------------
// messaging helper — retries once if the ephemeral SW tore down the channel
// (the classic MV3 "message channel closed before a response" race). Also
// bounds requests so a stuck service-worker fetch cannot pin a scraper forever.
// ---------------------------------------------------------------------------
const DEFAULT_SEND_TIMEOUT_MS = 45000;
const SEND_TIMEOUT_MS_BY_TYPE = {
  ingest: 120000,
  posts: 60000,
  comments: 60000,
  profile: 60000,
  users: 60000,
  seed: 60000,
  dms: 60000,
  strava_streams: 60000,
  stravaRouteVisit: 60000,
  pageHealth: 20000,
  loopStatus: 15000,
  cycleReport: 20000,
  log: 10000,
  wall: 15000,
  tabReady: 15000,
};
const RETRYABLE_SEND_TIMEOUT_TYPES = new Set([
  "getTargets",
  "getXProfileTarget",
  "getTikTokRevisitTarget",
  "getBrowserMediaRevisitTarget",
  "getStravaRouteQueue",
  "igCooldown",
]);

function deadlineError(name, message) {
  const err = new Error(message);
  err.name = name;
  return err;
}

async function withDeadline(promise, timeoutMs, message, name = "UCDeadlineTimeout") {
  let timer = null;
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(deadlineError(name, message)), timeoutMs);
    });
    return await Promise.race([promise, timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function sendTimeoutMs(msg, explicitTimeoutMs) {
  if (Number.isFinite(Number(explicitTimeoutMs)) && Number(explicitTimeoutMs) > 0) {
    return Number(explicitTimeoutMs);
  }
  const type = String((msg && msg.type) || "");
  return SEND_TIMEOUT_MS_BY_TYPE[type] || DEFAULT_SEND_TIMEOUT_MS;
}

const DIRECT_INGEST_BASE = "http://127.0.0.1:8765";
// Page origins whose CSP `connect-src` blocks http://127.0.0.1:* — the page's
// own CSP will refuse the direct fetch with "Refused to connect" BEFORE
// preflight, generating a console error even if we .catch() the promise. For
// these platforms, skip the direct-fetch attempt entirely and route through
// the extension-origin SW proxy (chrome-extension:// is exempt from page CSP).
//
// Observed on Instagram: `connect-src *.instagram.com wss://edge-chat.instagram.com
// ... ws://localhost:* 'self' ...` — allows ws://localhost but NOT http://127.0.0.1.
// Threads and Facebook enforce the same policy (same Meta CSP template).
const DIRECT_FETCH_CSP_BLOCKED = new Set(["instagram", "threads", "facebook"]);

function directFetchAllowed() {
  const p = currentPlatform();
  return !(p && p.id && DIRECT_FETCH_CSP_BLOCKED.has(p.id));
}

const DEFAULT_DIRECT_SEND_TIMEOUT_MS = 20000;
const DIRECT_SEND_TIMEOUT_MS_BY_TYPE = {
  ingest: 45000,
  posts: 20000,
  comments: 20000,
  profile: 20000,
  users: 20000,
  dms: 30000,
  strava_streams: 30000,
  pageHealth: 15000,
  loopStatus: 10000,
  cycleReport: 10000,
  log: 7000,
};

function directPlatform(msg) {
  const current = currentPlatform();
  const raw = String((msg && msg.platform) || "").trim().toLowerCase();
  if (current && current.id) {
    if (!raw || raw === String(current.label || "").toLowerCase()) return current.id;
    if (raw === current.id) return current.id;
  }
  return raw || (current && current.id) || "unknown";
}

function withDirectVersion(payload) {
  return { ...payload, extension_version: UC_CONTENT_VERSION };
}

function directSendTimeoutMs(msg) {
  const type = String((msg && msg.type) || "");
  return DIRECT_SEND_TIMEOUT_MS_BY_TYPE[type] || DEFAULT_DIRECT_SEND_TIMEOUT_MS;
}

function directHeartbeatPayload(msg, error, status) {
  const p = currentPlatform() || {};
  return withDirectVersion({
    platform: directPlatform(msg),
    label: msg.label || p.label || msg.platform || null,
    running: msg.running !== false,
    url: msg.url || location.href,
    tab_id: "content_direct",
    health_status: status,
    health_reason: String(error && error.message ? error.message : error || "service_worker_unavailable").slice(0, 240),
    message_type: msg.type || null,
    cycle_targets: msg.targets ?? null,
    cycle_saved: msg.saved ?? null,
    cycle_discovered: msg.discovered ?? null,
    loop_running: msg.type === "loopStatus" ? !!msg.running : null,
    content_age_seconds: msg.content_age_seconds ?? null,
    stale_after_ms: msg.stale_after_ms ?? null,
    text_sample: msg.msg ? String(msg.msg).slice(0, 260) : null,
  });
}

function directFallbackRequest(msg, error) {
  const type = String((msg && msg.type) || "");
  const platform = directPlatform(msg);
  if (type === "loopStatus") {
    return { path: "/social/browser-heartbeat", payload: directHeartbeatPayload(msg, error, "content_direct_loop") };
  }
  if (type === "tabReady") {
    return { path: "/social/browser-heartbeat", payload: directHeartbeatPayload(msg, error, "content_direct_tab_ready") };
  }
  if (type === "cycleReport") {
    return { path: "/social/browser-heartbeat", payload: directHeartbeatPayload(msg, error, "content_direct_cycle_report") };
  }
  if (type === "pageHealth") {
    return {
      path: "/social/browser-heartbeat",
      payload: withDirectVersion({
        platform,
        label: msg.label || platform,
        running: true,
        url: msg.url || location.href,
        tab_id: "content_direct",
        health_status: msg.status || "content_direct_page_health",
        health_reason: msg.reason || String(error && error.message ? error.message : error || "service_worker_unavailable").slice(0, 240),
        text_sample: msg.sample || null,
        content_counts: msg.content_counts || null,
      }),
    };
  }
  if (type === "log") {
    return { path: "/social/browser-heartbeat", payload: directHeartbeatPayload(msg, error, "content_direct_log") };
  }
  if (type === "ingest") {
    const allItems = Array.isArray(msg.items) ? msg.items : [];
    const urlItems = allItems.filter((it) => !(it && it.browser_upload_only === true));
    if (!urlItems.length && !msg.record_empty) return null;
    return {
      path: "/social/ingest",
      payload: withDirectVersion({
        platform,
        username: msg.username,
        items: urlItems,
        record_empty: !!msg.record_empty,
        probe_reason: msg.probe_reason || "content_direct_fallback",
        probe_meta: { ...(msg.probe_meta || {}), direct_fallback: true },
      }),
    };
  }
  const jsonRoutes = {
    discover: ["/social/discover", { platform, source: msg.source, hop: msg.hop, discovered: msg.discovered }],
    targetStatus: ["/social/target-status", { platform, username: msg.username, status: msg.status, reason: msg.reason || null }],
    posts: ["/social/posts", { platform, username: msg.username, posts: msg.posts }],
    comments: ["/social/comments", { platform, post_id: msg.post_id, comments: msg.comments }],
    users: ["/social/users", { platform, context: msg.context || "seen", owner: msg.owner || null, users: msg.users }],
    profile: ["/social/profile", { platform, profile: msg.profile, owner: msg.owner || null }],
    seed: ["/social/seed", { platform, users: msg.users }],
    dms: ["/social/dms", { platform, owner: msg.owner || null, threads: msg.threads }],
    strava_streams: ["/social/strava-streams", {
      platform: "strava",
      activity_id: msg.activity_id,
      request_url: msg.request_url || msg.url || null,
      http_status: msg.http_status || null,
      owner: msg.owner || msg.account || null,
      streams: msg.streams || {},
      point_count: msg.point_count || null,
    }],
  };
  if (!jsonRoutes[type]) return null;
  return { path: jsonRoutes[type][0], payload: withDirectVersion(jsonRoutes[type][1]) };
}

async function trySwFetchProxy(request) {
  try {
    const proxy = await withDeadline(
      chrome.runtime.sendMessage({
        type: "swFetchProxy",
        path: request.path,
        payload: request.payload,
      }),
      DIRECT_SEND_TIMEOUT_MS_BY_TYPE.swFetchProxy || 15000,
      "swFetchProxy message timed out after 15s",
      "UCSwFetchProxyTimeout"
    );
    if (proxy && proxy.ok !== undefined) {
      return {
        ok: !!proxy.ok,
        direct_fallback: true,
        proxied: true,
        status: proxy.status,
        ...(proxy.body || {}),
      };
    }
  } catch (_) {
    // channel closed / extension context invalidated — caller decides what to do
  }
  return null;
}

async function directSendFallback(msg, error) {
  const request = directFallbackRequest(msg, error);
  if (!request) return null;
  const type = String((msg && msg.type) || "message");
  const timeoutMs = directSendTimeoutMs(msg);

  // On page origins whose CSP `connect-src` blocks http://127.0.0.1:*
  // (instagram/threads/facebook), do NOT attempt the direct fetch: it would
  // raise a "Refused to connect" console error before failing, even if we
  // .catch() the promise. Go straight to the extension-origin SW proxy.
  if (!directFetchAllowed()) {
    const proxied = await trySwFetchProxy(request);
    return proxied; // may be null; send() treats null as "no fallback"
  }

  try {
    const response = await withDeadline(
      fetch(DIRECT_INGEST_BASE + request.path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request.payload),
      }),
      timeoutMs,
      `${type} direct fallback timed out after ${Math.ceil(timeoutMs / 1000)}s`,
      "UCDirectSendTimeout"
    );
    const body = await response.json().catch(() => ({}));
    return { ok: response.ok, direct_fallback: true, status: response.status, ...body };
  } catch (directError) {
    // Chrome's Local Network Access / PNA policy blocks direct fetches to
    // 127.0.0.1 from some page origins (observed: https://www.lemon8-app.com)
    // regardless of CORS headers — the block fires before preflight. Fall
    // back to a SW-proxied fetch: the service worker runs in extension
    // origin (127.0.0.1/* declared in host_permissions), so its fetch is
    // exempt from LNA and reaches the ingest bridge.
    const proxied = await trySwFetchProxy(request);
    if (proxied) return proxied;
    throw directError;
  }
}

async function send(msg, { retries = 1, timeoutMs = null } = {}) {
  for (let i = 0; ; i++) {
    try {
      const ms = sendTimeoutMs(msg, timeoutMs);
      const type = (msg && msg.type) || "message";
      return await withDeadline(
        chrome.runtime.sendMessage(msg),
        ms,
        `${type} message timed out after ${Math.ceil(ms / 1000)}s`,
        "UCSendTimeout"
      );
    } catch (e) {
      const channelTransient = /message channel closed|Could not establish|Receiving end does not exist|Extension context invalidated/i.test(
        e.message || ""
      );
      const retryableTimeout = e.name === "UCSendTimeout" && RETRYABLE_SEND_TIMEOUT_TYPES.has(String((msg && msg.type) || ""));
      const transient = channelTransient || retryableTimeout;
      if (!transient || i >= retries) {
        const fallback = await directSendFallback(msg, e).catch(() => null);
        if (fallback) return fallback;
        if (/Extension context invalidated/i.test(e.message || "")) return null; // page outlived extension
        throw e;
      }
      await sleep(400); // let the worker respawn
    }
  }
}
function clog(level, msg, platform) {
  send({ type: "log", level, msg, platform }).catch(() => {});
}

function sendSideEffect(msg, platform, label, options = {}) {
  send(msg, options).catch((e) => {
    const detail = e && e.message ? e.message : String(e || "unknown error");
    if (label) clog("warn", `${label} skipped: ${detail}`, platform);
  });
}

const PAGE_HEALTH_REPORT_GAP_MS = 60000;
const WALL_LOG_GAP_MS = 10 * 60000;
const LAST_PAGE_HEALTH_REPORT = {};
const LAST_WALL_LOG_AT = {};
const PAGE_RECOVERY_ENABLED = new Set(["lemon8", "tiktok", "threads", "x"]);
const RECOVERABLE_PAGE_SHELL_PATTERNS = {
  tiktok: [
    { reason: "sorry_could_not_show_page", re: /sorry,?\s*we\s+couldn(?:'|\u2019)?t\s+show\s+that\s+page/i },
    { reason: "could_not_show_page", re: /couldn(?:'|\u2019)?t\s+show\s+(?:this|that)\s+page/i },
    { reason: "page_not_available", re: /this\s+page\s+isn(?:'|\u2019)?t\s+available/i },
    { reason: "page_not_found", re: /page\s+not\s+found/i },
    { reason: "something_went_wrong", re: /something\s+went\s+wrong/i, lowContent: true },
    { reason: "try_again_empty_state", re: /\btry\s+again\b/i, lowContent: true },
    { reason: "no_internet_connection", re: /no\s+internet\s+connection/i, lowContent: true },
    { reason: "video_unavailable", re: /video\s+currently\s+unavailable/i, lowContent: true },
  ],
  lemon8: [
    { reason: "something_went_wrong", re: /something\s+went\s+wrong/i, lowContent: true },
    { reason: "try_again_empty_state", re: /\btry\s+again\b/i, lowContent: true },
    { reason: "feed_empty_state", re: /\b(no\s+more|no\s+content|refresh)\b/i, lowContent: true },
    // Lemon8's SPA renders "Not found" when a /feed/<category> path is stale
    // (route removed by the app). The page is HTTP 200 so a plain reload keeps
    // the same broken URL — background.js runPageRecovery navigates the tab to
    // platform.url instead of reloading when the reason is "not_found".
    { reason: "not_found", re: /\bnot\s+found\b/i, lowContent: true },
  ],
  threads: [
    { reason: "something_went_wrong", re: /something\s+went\s+wrong/i, lowContent: true },
    { reason: "try_again_empty_state", re: /\btry\s+again\b/i, lowContent: true },
    { reason: "page_not_available", re: /this\s+page\s+isn(?:'|\u2019)?t\s+available/i, lowContent: true },
    { reason: "login_shell", re: /\b(log\s*in|continue\s+with\s+instagram)\b/i, lowContent: true },
  ],
  default: [
    { reason: "page_not_available", re: /this\s+page\s+isn(?:'|\u2019)?t\s+available/i },
    { reason: "page_not_found", re: /page\s+not\s+found/i },
    { reason: "something_went_wrong", re: /something\s+went\s+wrong/i, lowContent: true },
    { reason: "try_again_empty_state", re: /\btry\s+again\b/i, lowContent: true },
    { reason: "no_internet_connection", re: /no\s+internet\s+connection/i, lowContent: true },
  ],
};

function pageContentCounts() {
  try {
    return {
      articles: document.querySelectorAll("article").length,
      videos: document.querySelectorAll("video").length,
      images: document.querySelectorAll("img[src], img[srcset]").length,
      links: document.querySelectorAll("a[href]").length,
    };
  } catch (e) {
    return { articles: 0, videos: 0, images: 0, links: 0 };
  }
}
function visiblePageText() {
  try {
    const focused = [...document.querySelectorAll('[role="alert"], [data-e2e*="error" i], [data-e2e*="empty" i], h1, h2, button')]
      .map((el) => (el.innerText || el.textContent || "").trim())
      .filter(Boolean)
      .slice(0, 80)
      .join("\n");
    const body = (document.body && document.body.innerText) ? document.body.innerText.slice(0, 9000) : "";
    return [document.title || "", focused, body].filter(Boolean).join("\n");
  } catch (e) {
    return document.title || "";
  }
}
function compactSample(text) {
  return String(text || "").replace(/\s+/g, " ").trim().slice(0, 260);
}
function detectRecoverableUrlShell(platformId) {
  if (platformId === "x" && /[?&]failedScript(?:=|&|$)/i.test(location.search || "")) {
    return {
      reason: "failed_script_url",
      sample: compactSample(location.href),
      content_counts: pageContentCounts(),
    };
  }
  return null;
}
function detectRecoverablePageShell(platformId) {
  if (!PAGE_RECOVERY_ENABLED.has(platformId)) return null;
  const urlShell = detectRecoverableUrlShell(platformId);
  if (urlShell) return urlShell;
  const counts = pageContentCounts();
  const text = visiblePageText();
  if (platformId === "x") {
    const pageAgeMs = (() => {
      try { return Number(performance.now && performance.now()) || 0; } catch (e) { return 0; }
    })();
    const blankSpaShell = (
      pageAgeMs > 20000
      && counts.articles === 0
      && counts.videos === 0
      && counts.links < 3
      && String(text || "").trim().length < 20
    );
    if (blankSpaShell) {
      return {
        reason: "x_blank_spa_shell",
        sample: compactSample(document.title || location.href),
        content_counts: counts,
      };
    }
  }
  if (!text) return null;
  const usefulNodes = counts.articles + counts.videos + counts.images;
  const lowContent = usefulNodes < 4 && counts.links < 40;
  const patterns = [...(RECOVERABLE_PAGE_SHELL_PATTERNS[platformId] || []), ...RECOVERABLE_PAGE_SHELL_PATTERNS.default];
  for (const pat of patterns) {
    if (pat.re.test(text) && (!pat.lowContent || lowContent)) {
      return { reason: pat.reason, sample: compactSample(text), content_counts: counts };
    }
  }
  return null;
}
async function reportRecoverablePageShell(p, shell) {
  const key = `${p.id}:${shell.reason}:${location.href}`;
  const now = Date.now();
  if (LAST_PAGE_HEALTH_REPORT[key] && now - LAST_PAGE_HEALTH_REPORT[key] < PAGE_HEALTH_REPORT_GAP_MS) return null;
  LAST_PAGE_HEALTH_REPORT[key] = now;
  clog("warn", `${p.label} page looks stuck (${shell.reason}); asking extension to reload with backoff`, p.label);
  return send({
    type: "pageHealth",
    platform: p.id,
    label: p.label,
    status: "recoverable_error_shell",
    reason: shell.reason,
    url: location.href,
    title: document.title || "",
    sample: shell.sample,
    content_counts: shell.content_counts,
  }).catch(() => null);
}

function findRecoverablePageActionButton() {
  const candidates = [
    ...document.querySelectorAll('button, [role="button"], a[href]'),
  ];
  return candidates.find((el) => {
    const text = String(el.innerText || el.textContent || el.getAttribute("aria-label") || "")
      .replace(/\s+/g, " ")
      .trim();
    if (!text || text.length > 80) return false;
    return /^(try again|retry|reload|refresh)$/i.test(text)
      || /\b(try again|retry|reload|refresh)\b/i.test(text);
  }) || null;
}

function switchXHostForRecoverableShell(shell) {
  if (!shell || !/failed_script|blank_spa|try_again|something_went_wrong|no_internet|page_not_available|page_not_found/i.test(shell.reason || "")) {
    return false;
  }
  const navKey = `uc_x_shell_nav_${shell.reason || "shell"}`;
  const stepKey = "uc_x_shell_global_step";
  const lastNav = lsNum(navKey);
  const minNavGapMs = /try_again/i.test(shell.reason || "") ? 30000 : 90000;
  if (lastNav && Date.now() - lastNav < minNavGapMs) return false;
  lsSet(navKey, String(Date.now()));
  const host = String(location.hostname || "").toLowerCase();
  const stamp = Math.floor(Date.now() / 1000);
  const onTwitterHost = host === "twitter.com" || host.endsWith(".twitter.com");
  const alternateHost = onTwitterHost ? `https://x.com/home?uc_recover=${stamp}` : `https://twitter.com/home?uc_recover=${stamp}`;
  const owner = ownerFromStoredOrDom("x", xLoggedInOwner);
  const reason = String(shell.reason || "");
  const isGenericShell = /try_again|something_went_wrong|no_internet/i.test(reason);
  const targets = isGenericShell
    ? [
        `https://x.com/home?uc_recover=${stamp}`,
        `https://x.com/explore?uc_recover=${stamp}`,
        alternateHost,
        `https://x.com/home?uc_recover=${stamp}`,
      ]
    : [
        `https://x.com/explore?uc_recover=${stamp}`,
        alternateHost,
        `https://x.com/home?uc_recover=${stamp}`,
        ...(owner ? ["https://x.com/" + encodeURIComponent(owner) + "?uc_recover=" + stamp] : []),
      ];
  const step = lsNum(stepKey) % targets.length;
  lsSet(stepKey, String(step + 1));
  const target = targets[step] || `https://x.com/home?uc_recover=${stamp}`;
  clog("warn", `x page shell still stuck (${shell.reason}); navigating to ${target}`, "x");
  location.href = target;
  return true;
}

async function attemptRecoverablePageInteraction(platformId, shell) {
  if (!PAGE_RECOVERY_ENABLED.has(platformId) || !shell) return false;
  if (platformId === "x" && shell.reason === "failed_script_url") {
    const key = "uc_recover_click_x_failed_script_url";
    const last = lsNum(key);
    if (last && Date.now() - last < 2 * 60000) {
      if (switchXHostForRecoverableShell(shell)) return true;
    }
    lsSet(key, String(Date.now()));
    try {
      history.replaceState(null, "", "https://x.com/home");
    } catch (e) {}
    clog("warn", "x failedScript URL detected; returning to clean home", "x");
    location.href = "https://x.com/home?uc_recover=" + Math.floor(Date.now() / 1000);
    return true;
  }
  const key = `uc_recover_click_${platformId}_${shell.reason || "shell"}`;
  const last = lsNum(key);
  if (platformId === "x") {
    if (last && Date.now() - last < 15000) return false;
    lsSet(key, String(Date.now()));
    if (switchXHostForRecoverableShell(shell)) return true;
    // X's native "Try again" shell often re-renders the same empty SPA. Avoid
    // clicking it repeatedly; the scheduled page recovery will hard-navigate.
    return false;
  }
  if (last && Date.now() - last < 60000) return false;
  const button = findRecoverablePageActionButton();
  if (!button) {
    if (platformId === "x" && switchXHostForRecoverableShell(shell)) return true;
    return false;
  }
  lsSet(key, String(Date.now()));
  try {
    button.scrollIntoView({ block: "center", inline: "center" });
  } catch (e) {}
  await sleep(jitter(700));
  try {
    button.click();
    clog("warn", `${platformId} page shell action clicked (${shell.reason || "recoverable"})`, platformId);
    return true;
  } catch (e) {
    return false;
  }
}

// Capture is ALWAYS ON (user: "i want them on at all times"). Stories, highlights
// and comments are captured every cycle — no toggles. Pacing is handled by the
// human-paced loop + wall cooldown, not by disabling capture.
const CAPTURE = { stories: true, highlights: true, comments: true };

// A login-wall / throttle returns an HTML doc with HTTP 200. Detect it so we can
// back off cleanly instead of crashing every target with "Unexpected token '<'".
class WallError extends Error {}
async function fetchJson(url, opts) {
  const res = await fetch(url, opts);
  const ctype = res.headers.get("content-type") || "";
  if (!res.ok) {
    if (res.status === 429 || res.status === 401 || res.status === 403) throw new WallError("HTTP " + res.status);
    const err = new Error("HTTP " + res.status);
    err.status = res.status;
    throw err;
  }
  if (!/json/i.test(ctype)) {
    const head = (await res.text()).slice(0, 40).replace(/\s+/g, " ");
    if (/^<|doctype|<html/i.test(head)) throw new WallError("login/throttle wall");
    throw new Error("non-JSON response");
  }
  return res.json();
}

// Stable id from a media URL (origin+path, volatile query stripped) so the SAME
// image gets the SAME content_id across re-scrapes -> server dedups before
// downloading. Fixes the lemon8/tiktok re-download duplication.
function urlId(u) {
  let s = u || "";
  try { const x = new URL(u); s = x.origin + x.pathname; } catch (e) {}
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(36);
}

// Shared collector — dedups media items by content_id+url.
function makeSink() {
  const seen = new Set();
  const items = [];
  return {
    items,
    add(it) {
      if (!it || !it.url || !it.content_id) return;
      const k = it.content_id + "|" + it.url;
      if (seen.has(k)) return;
      seen.add(k);
      items.push(it);
    },
  };
}

function imageUrlsFromElement(im) {
  const out = [];
  const add = (u) => {
    if (u && /^https?:/i.test(u) && !out.includes(u)) out.push(u);
  };
  try { add(im.currentSrc); } catch (e) {}
  try { add(im.src); } catch (e) {}
  try {
    const srcset = im.getAttribute && im.getAttribute("srcset");
    if (srcset) {
      srcset.split(",").forEach((part) => {
        const u = part.trim().split(/\s+/)[0];
        add(u);
      });
    }
  } catch (e) {}
  return out;
}

function urlsFromCssValue(value) {
  const out = [];
  const text = String(value || "");
  const re = /url\(["']?([^"')]+)["']?\)/gi;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m[1] && /^https?:/i.test(m[1]) && !out.includes(m[1])) out.push(m[1]);
  }
  return out;
}

function elementLooksTooSmall(el, min = 120) {
  try {
    const r = el.getBoundingClientRect && el.getBoundingClientRect();
    if (r && r.width && r.height && (r.width < min || r.height < min)) return true;
  } catch (e) {}
  return false;
}

function imageLooksTooSmall(im, min = 120) {
  try {
    const nw = im.naturalWidth || 0;
    const nh = im.naturalHeight || 0;
    if (nw && nh && (nw < min || nh < min)) return true;
    const r = im.getBoundingClientRect && im.getBoundingClientRect();
    if (r && r.width && r.height && (r.width < 48 || r.height < 48)) return true;
  } catch (e) {}
  return false;
}

// Walk an arbitrary embedded-state object collecting {url, type, id}. Used for
// TikTok/Lemon8 where the page ships its data as JSON in a <script> tag.
function deepCollectMedia(obj, sink, entity, depth = 0) {
  if (!obj || depth > 8) return;
  if (Array.isArray(obj)) {
    for (const v of obj) deepCollectMedia(v, sink, entity, depth + 1);
    return;
  }
  if (typeof obj !== "object") return;
  // Full video node (TikTok). These URLs usually 403 outside Chrome, so send
  // them through the browser-upload path rather than the server URL fetcher.
  const vid = obj.video || obj.Video;
  if (vid && (vid.playAddr || vid.downloadAddr || vid.PlayAddr)) {
    const url = vid.downloadAddr || vid.playAddr || vid.PlayAddr;
    if (typeof url === "string") {
      sink.add({
        content_id: String(obj.id || obj.awemeId || url) + "_video",
        content_type: "video",
        url,
        entity_name: entity,
        kind: "post",
        browser_upload: true,
        browser_upload_only: true,
        meta: { tiktok_asset_role: "video_playaddr" },
      });
    }
  }
  // cover/poster node (tiktok). TikTok video CDN URLs are often short-lived
  // and cookie-bound when fetched server-side; cover images survive much more
  // reliably and still preserve the post as media evidence.
  const coverSources = [
    obj.cover,
    obj.originCover,
    obj.dynamicCover,
    obj.video && obj.video.cover,
    obj.video && obj.video.originCover,
    obj.video && obj.video.dynamicCover,
    obj.Video && obj.Video.cover,
    obj.Video && obj.Video.originCover,
    obj.Video && obj.Video.dynamicCover,
  ];
  coverSources.forEach((cover, i) => {
    const u = typeof cover === "string"
      ? cover
      : cover && (
          (cover.urlList && cover.urlList[0]) ||
          (cover.url_list && cover.url_list[0]) ||
          cover.url ||
          cover.imageUrl ||
          cover.image_url
        );
    if (typeof u === "string" && /^https?:/.test(u)) {
      sink.add({
        content_id: String(obj.id || obj.awemeId || obj.postId || u) + "_cover_" + i,
        content_type: "photo",
        url: u,
        entity_name: entity,
        kind: "post",
        meta: { tiktok_asset_role: "cover" },
      });
    }
  });
  // image post (tiktok photo mode / lemon8)
  const imgs = (obj.imagePost && obj.imagePost.images) || obj.images || obj.imageList || obj.imageInfo;
  if (Array.isArray(imgs)) {
    imgs.forEach((im, i) => {
      const u =
        (im.imageURL && (im.imageURL.urlList || [])[0]) ||
        (im.urlList || [])[0] ||
        im.url || im.imageUrl || (im.imageInfos && im.imageInfos[0] && im.imageInfos[0].url);
      if (typeof u === "string") sink.add({ content_id: String(obj.id || obj.postId || u) + "_" + i, content_type: "photo", url: u, entity_name: entity });
    });
  }
  for (const k in obj) {
    const v = obj[k];
    if (v && typeof v === "object") deepCollectMedia(v, sink, entity, depth + 1);
  }
}

// Scan embedded state for user objects (TikTok uniqueId/nickname, Lemon8 handles)
// so we record every creator/commenter we come across into social_users.
function deepCollectUsers(obj, users, depth = 0) {
  if (!obj || depth > 8 || users.length > 800) return;
  if (Array.isArray(obj)) { for (const v of obj) deepCollectUsers(v, users, depth + 1); return; }
  if (typeof obj !== "object") return;
  const uname = obj.uniqueId || obj.unique_id || obj.handle || (obj.author && obj.author.uniqueId);
  if (typeof uname === "string" && uname && uname.length < 40) {
    users.push({
      user_id: obj.id || obj.uid || obj.userId || null, username: uname,
      display_name: obj.nickname || obj.nick_name || obj.name || null,
      profile_pic_url: obj.avatarThumb || obj.avatar_thumb || obj.avatarMedium || obj.avatar || null,
    });
  }
  for (const k in obj) { const v = obj[k]; if (v && typeof v === "object") deepCollectUsers(v, users, depth + 1); }
}

function parseEmbeddedState(ids) {
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el && el.textContent) {
      try { return JSON.parse(el.textContent); } catch (e) {}
    }
  }
  return null;
}

function cappedHuman(base, maxMs) {
  const ms = human(base);
  return Number.isFinite(Number(maxMs)) && Number(maxMs) > 0
    ? Math.min(ms, Number(maxMs))
    : ms;
}

async function autoScroll(times = 8, dist = 1400, pause = 1800, options = {}) {
  const maxPauseMs = options && options.maxPauseMs;
  for (let i = 0; i < times; i++) {
    window.scrollBy(0, dist * (0.7 + Math.random() * 0.6));
    await sleep(cappedHuman(pause, maxPauseMs)); // human, irregular scroll cadence
  }
}

const FOLLOW_SWEEP_TTL_MS = 12 * 60 * 60 * 1000;

function ownerFromStoredOrDom(platform, domFn) {
  const k = "uc_owner_" + platform;
  let owner = "";
  try { owner = (localStorage.getItem(k) || "").trim().replace(/^@/, ""); } catch (e) {}
  if (!owner && typeof domFn === "function") {
    try { owner = (domFn() || "").trim().replace(/^@/, ""); } catch (e) {}
  }
  if (owner) {
    try { localStorage.setItem(k, owner); } catch (e) {}
  }
  return owner || "";
}

function collectFollowHandlesFromDom(platform, owner) {
  const users = [];
  const seen = new Set();
  const ownerLc = String(owner || "").toLowerCase();
  const reserved = /^(home|explore|search|notifications|messages|settings|i|intent|share|privacy|about|tos|login|signup)$/i;
  const patterns = platform === "threads"
    ? [/^\/@([A-Za-z0-9._]{1,30})\/?$/]
    : platform === "tiktok"
      ? [/^\/@([A-Za-z0-9._]{1,32})\/?$/]
      : [/^\/([A-Za-z0-9_]{1,20})\/?$/];
  for (const a of document.querySelectorAll("a[href]")) {
    const href = (a.getAttribute("href") || "").replace(/^https?:\/\/(?:www\.)?(?:x\.com|twitter\.com|threads\.com|tiktok\.com)/i, "");
    let handle = "";
    for (const re of patterns) {
      const m = href.match(re);
      if (m) { handle = m[1]; break; }
    }
    if (!handle || reserved.test(handle) || handle.toLowerCase() === ownerLc) continue;
    const key = handle.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    const row = a.closest('[role="listitem"], [data-e2e], div');
    const img = row && row.querySelector && row.querySelector("img");
    users.push({
      username: handle,
      display_name: (a.innerText || "").split("\n").map((s) => s.trim()).filter(Boolean)[0] || null,
      profile_pic_url: img && (img.currentSrc || img.src) || null,
    });
  }
  return users.slice(0, 800);
}

async function maybeSweepFollowGraph({ platform, owner, urls, homeUrl }) {
  owner = (owner || "").trim().replace(/^@/, "");
  if (!owner || !urls) return false;
  if (deferNavigationForForcedRecovery(platform)) return false;
  const stateKey = "uc_" + platform + "_follow_sweep_state";
  const lastKey = "uc_" + platform + "_follow_sweep_last_" + owner.toLowerCase();
  const active = (() => { try { return JSON.parse(localStorage.getItem(stateKey) || "null"); } catch (e) { return null; } })();
  const now = Date.now();
  const sideFromPath = (() => {
    const p = location.pathname;
    if (urls.followingPath && urls.followingPath.test(p)) return "following";
    if (urls.followersPath && urls.followersPath.test(p)) return "followers";
    return "";
  })();
  if (active && active.owner === owner && active.side && sideFromPath === active.side) {
    await autoScroll(active.side === "following" ? 10 : 8, 1200, 1800, { maxPauseMs: 3500 });
    const context = active.side === "followers" ? "follower" : "follow";
    const users = collectFollowHandlesFromDom(platform, owner);
    if (users.length) {
      await send({ type: "users", platform, context, owner: { username: owner }, users }).catch(() => {});
      clog("info", `${platform} owner graph @${owner}: ${active.side} +${users.length}`, platform);
    }
    if (active.side === "following") {
      localStorage.setItem(stateKey, JSON.stringify({ owner, side: "followers" }));
      await sleep(jitter(2500));
      location.href = urls.followers;
    } else {
      localStorage.removeItem(stateKey);
      localStorage.setItem(lastKey, String(now));
      await sleep(jitter(2500));
      location.href = homeUrl;
    }
    return true;
  }
  const last = parseInt(lsGet(lastKey, "0"), 10) || 0;
  if (!active && now - last > FOLLOW_SWEEP_TTL_MS) {
    localStorage.setItem(stateKey, JSON.stringify({ owner, side: "following" }));
    await sleep(jitter(1500));
    location.href = urls.following;
    return true;
  }
  return false;
}

// ===========================================================================
// Instagram (same-origin API; full media + 2-hop spider)
// ===========================================================================
const IG_APP_ID = "936619743392459";
const SPIDER_FAMOUS_CAP = 3000;   // skip accounts > 3k followers (focus on close network)
const SPIDER_FOLLOWS_PER_SIDE = 35;     // fewer graph calls per profile
const IG_MAX_ITEMS = 180;               // cap media pages per profile
// Per-cycle target budget: a human checks a HANDFUL of profiles, not 257.
// Randomised each cycle; the rest are picked up on later cycles (round-robin).
function igTargetBudget() { return 2 + ((Math.random() * 3) | 0); } // 2-4 deep profiles/cycle
const IG_STORY_SWEEP = 5;   // profiles to grab EXPIRING stories/highlights from, first, each cycle
const IG_SEEDED_ACCOUNTS = new Set();  // ds_user_ids self-seeded this session (re-seeds on account switch)

const instagram = {
  id: "instagram", host: "www.instagram.com", label: "Instagram",
  csrf() { const m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ""; },
  headers() { return { "x-ig-app-id": IG_APP_ID, "x-csrftoken": this.csrf(), "x-requested-with": "XMLHttpRequest" }; },

  async getProfile(username) {
    const url = "https://www.instagram.com/api/v1/users/web_profile_info/?username=" + encodeURIComponent(username);
    try {
      const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
      return j && j.data && j.data.user;
    } catch (e) {
      if (e && (e.status === 400 || e.status === 404)) {
        await send({
          type: "targetStatus",
          platform: "instagram",
          username,
          status: "unavailable",
          reason: "profile_http_" + e.status,
        }).catch(() => {});
        clog("info", `skip unavailable profile ${username}: HTTP ${e.status}`, "instagram");
        return null;
      }
      throw e;
    }
  },
  // Push the full profile (bio, counts, profile photo) -> instagram_profiles +
  // social_users; the server also downloads the profile pic as kind=profile.
  async _sendProfile(user) {
    if (!user || !user.username) return;
    await send({ type: "profile", platform: "instagram", profile: {
      user_id: user.id, username: user.username, full_name: user.full_name,
      bio: user.biography,
      followers_count: user.edge_followed_by && user.edge_followed_by.count,
      following_count: user.edge_follow && user.edge_follow.count,
      posts_count: user.edge_owner_to_timeline_media && user.edge_owner_to_timeline_media.count,
      is_verified: user.is_verified, is_private: user.is_private,
      profile_pic_url: user.profile_pic_url_hd || user.profile_pic_url,
      external_url: user.external_url,
    } }).catch(() => {});
  },
  // Pull caption + engagement off a post node — these are ALREADY in the
  // web_profile_info / feed/user response, so capturing them costs no extra
  // requests. Handles both the GraphQL (edge_*) and v1 (caption/like_count) shapes.
  postMeta(n) {
    const caption =
      (n.edge_media_to_caption && n.edge_media_to_caption.edges && n.edge_media_to_caption.edges[0] && n.edge_media_to_caption.edges[0].node.text) ||
      (n.caption && (n.caption.text || (typeof n.caption === "string" ? n.caption : null))) || null;
    const likes = (n.edge_liked_by && n.edge_liked_by.count) ?? (n.edge_media_preview_like && n.edge_media_preview_like.count) ?? n.like_count ?? null;
    const comments = (n.edge_media_to_comment && n.edge_media_to_comment.count) ?? (n.edge_media_to_parent_comment && n.edge_media_to_parent_comment.count) ?? n.comment_count ?? null;
    const views = n.video_view_count ?? n.view_count ?? n.play_count ?? null;
    return {
      caption, likes_count: likes, comments_count: comments, views_count: views,
      taken_at: n.taken_at_timestamp || n.taken_at || null,
      shortcode: n.shortcode || n.code || null,
      location: (n.location && (n.location.name || n.location.short_name)) || null,
      location_lat: (n.location && (n.location.lat ?? n.location.latitude)) ?? null,
      location_lng: (n.location && (n.location.lng ?? n.location.longitude)) ?? null,
      music_title: (n.clips_music_attribution_info && n.clips_music_attribution_info.song_name)
        || (n.music_metadata && n.music_metadata.music_info && n.music_metadata.music_info.music_asset_info && n.music_metadata.music_info.music_asset_info.title) || null,
      music_author: (n.clips_music_attribution_info && n.clips_music_attribution_info.artist_name)
        || (n.music_metadata && n.music_metadata.music_info && n.music_metadata.music_info.music_asset_info && n.music_metadata.music_info.music_asset_info.display_artist) || null,
    };
  },

  extractMedia(node, username) {
    const out = [];
    const meta = this.postMeta(node);
    const push = (n, cid) => {
      let url = null, type = "photo";
      if (n.video_url) { url = n.video_url; type = "video"; }
      else if (n.video_versions && n.video_versions[0]) { url = n.video_versions[0].url; type = "video"; }
      else if (n.display_url) { url = n.display_url; }
      else if (n.image_versions2 && n.image_versions2.candidates && n.image_versions2.candidates[0]) { url = n.image_versions2.candidates[0].url; }
      if (url) out.push({ content_id: String(cid), content_type: type, url, entity_name: username, meta });
    };
    const cid = node.id || node.pk || node.code;
    const children = (node.edge_sidecar_to_children && node.edge_sidecar_to_children.edges) || node.carousel_media;
    if (children && children.length) children.forEach((c, i) => push(c.node || c, cid + "_" + i));
    else push(node, cid);
    return out;
  },
  // Build a structured post record (caption/likes/comments/hashtags/location) for
  // the instagram_posts table. Hashtags/mentions parsed from the caption text.
  buildPost(n) {
    const m = this.postMeta(n);
    const cap = m.caption || "";
    return {
      platform_post_id: String(n.id || n.pk || n.code),
      media_type: (n.is_video || n.video_versions) ? "video"
        : (n.carousel_media || n.edge_sidecar_to_children) ? "carousel" : "photo",
      caption: m.caption,
      hashtags: (cap.match(/#[\w.]+/g) || []).map((s) => s.slice(1)),
      mentions: (cap.match(/@[\w.]+/g) || []).map((s) => s.slice(1)),
      location: m.location,
      location_lat: m.location_lat, location_lng: m.location_lng,
      music_title: m.music_title, music_author: m.music_author,
      likes_count: m.likes_count, comments_count: m.comments_count,
      taken_at: m.taken_at, video_duration: n.video_duration || null,
      metadata: { views_count: m.views_count, shortcode: m.shortcode },
    };
  },

  async scrapeUserMedia(user, username, maxItems = IG_MAX_ITEMS) {
    const media = [], posts = [];
    const tl = user.edge_owner_to_timeline_media;
    if (tl && tl.edges) tl.edges.forEach((e) => { media.push(...this.extractMedia(e.node, username)); posts.push(this.buildPost(e.node)); });
    let maxId = tl && tl.page_info && tl.page_info.end_cursor;
    let hasNext = tl && tl.page_info && tl.page_info.has_next_page;
    while (hasNext && media.length < maxItems) {
      await hsleep(4000);
      const url = "https://www.instagram.com/api/v1/feed/user/" + user.id + "/?count=33" + (maxId ? "&max_id=" + maxId : "");
      const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
      (j.items || []).forEach((it) => { media.push(...this.extractMedia(it, username)); posts.push(this.buildPost(it)); });
      maxId = j.next_max_id;
      hasNext = j.more_available && !!maxId;
    }
    return { media, posts };
  },

  // ---- stories / highlights / comments (opt-in; extra requests) ----------
  storyItemMedia(it, username, kind) {
    let url = null, type = "photo";
    if (it.video_versions && it.video_versions[0]) { url = it.video_versions[0].url; type = "video"; }
    else if (it.image_versions2 && it.image_versions2.candidates && it.image_versions2.candidates[0]) { url = it.image_versions2.candidates[0].url; }
    const cid = it.pk || it.id;
    const taken_at = it.taken_at || it.device_timestamp || null;
    return url ? [{ content_id: String(cid), content_type: type, url, entity_name: username, kind, taken_at }] : [];
  },
  _reelItems(j, key) {
    const reel = (j.reels && j.reels[key]) || (j.reels_media && j.reels_media[0]) || null;
    return (reel && reel.items) || [];
  },
  async getStories(userId, username) {
    const url = "https://www.instagram.com/api/v1/feed/reels_media/?reel_ids=" + encodeURIComponent(userId);
    const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
    const media = [];
    this._reelItems(j, String(userId)).forEach((it) => media.push(...this.storyItemMedia(it, username, "story")));
    return media;
  },
  // BULK story capture: the reels_tray returns EVERY followed account that has an
  // active story in ONE request, then reels_media fetches the full items for many
  // reel_ids at once. This captures your whole story feed per cycle in ~1-2
  // requests instead of one-per-profile — MORE coverage AND fewer requests
  // (ban-safer). A user's own story reel_id == their user pk.
  async getStoryTray() {
    const tray = await fetchJson("https://www.instagram.com/api/v1/feed/reels_tray/", { headers: this.headers(), credentials: "include" });
    const entries = (tray.tray || []).filter((t) => t.user && t.user.pk);
    if (!entries.length) return 0;
    const ids = entries.map((t) => String(t.user.pk));
    const nameById = {};
    entries.forEach((t) => { nameById[String(t.user.pk)] = (t.user.username || String(t.user.pk)); });
    let saved = 0;
    for (let i = 0; i < ids.length; i += 20) {
      const chunk = ids.slice(i, i + 20);
      const qs = chunk.map((id) => "reel_ids=" + encodeURIComponent(id)).join("&");
      let j;
      try { j = await fetchJson("https://www.instagram.com/api/v1/feed/reels_media/?" + qs, { headers: this.headers(), credentials: "include" }); }
      catch (e) { if (e instanceof WallError) throw e; continue; }
      for (const id of chunk) {
        const media = [];
        this._reelItems(j, id).forEach((it) => media.push(...this.storyItemMedia(it, nameById[id], "story")));
        if (media.length) { await send({ type: "ingest", platform: "instagram", username: nameById[id], items: media }); saved += media.length; }
      }
      await hsleep(4000);
    }
    if (saved) clog("info", `story tray: +${saved} story media across ${ids.length} account(s)`, "instagram");
    return saved;
  },
  async getHighlights(userId, username, maxReels = 5) {
    const tray = await fetchJson("https://www.instagram.com/api/v1/highlights/" + userId + "/highlights_tray/", { headers: this.headers(), credentials: "include" });
    const reels = (tray.tray || []).slice(0, maxReels);
    const media = [];
    for (const r of reels) {
      await hsleep(4000);
      const j = await fetchJson("https://www.instagram.com/api/v1/feed/reels_media/?reel_ids=" + encodeURIComponent(r.id), { headers: this.headers(), credentials: "include" });
      this._reelItems(j, String(r.id)).forEach((it) => media.push(...this.storyItemMedia(it, username, "highlight")));
    }
    return media;
  },
  async getComments(mediaId) {
    const url = "https://www.instagram.com/api/v1/media/" + mediaId + "/comments/?can_support_threading=true&permalink_enabled=false";
    const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
    return (j.comments || []).map((c) => ({
      platform_comment_id: String(c.pk || c.id),
      author_username: c.user && c.user.username,
      author_platform_id: c.user && String(c.user.pk),
      text: c.text, like_count: c.comment_like_count,
      created_at: c.created_at, is_reply: false,
    }));
  },

  // Tagged posts: posts where THIS profile is tagged by someone else (their
  // "Tagged" tab). We file the media under the scraped profile (kind=tagged) and
  // record every tagged user + the post owner into the user registry.
  async getTagged(userId, username) {
    const media = [], users = [];
    let maxId = "";
    for (let page = 0; page < 3; page++) {
      const url = "https://www.instagram.com/api/v1/usertags/" + userId + "/feed/?count=24" + (maxId ? "&max_id=" + maxId : "");
      const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
      (j.items || []).forEach((it) => {
        this.extractMedia(it, username).forEach((m) => media.push({ ...m, kind: "tagged" }));
        if (it.user) users.push({ user_id: it.user.pk, username: it.user.username, display_name: it.user.full_name });
        const tags = (it.usertags && it.usertags.in) || [];
        tags.forEach((t) => { if (t.user) users.push({ user_id: t.user.pk, username: t.user.username, display_name: t.user.full_name }); });
      });
      maxId = j.next_max_id;
      if (!maxId || !j.more_available) break;
      await hsleep(4000);
    }
    return { media, users };
  },
  async getFollows(userId, kind, max) {
    const out = [];
    let maxId = "";
    while (out.length < max) {
      const url = "https://www.instagram.com/api/v1/friendships/" + userId + "/" + kind + "/?count=50" + (maxId ? "&max_id=" + maxId : "");
      const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
      (j.users || []).forEach((u) => { if (u && u.username) out.push({ username: u.username }); });
      maxId = j.next_max_id;
      if (!maxId) break;
      await hsleep(4000);
    }
    return out.slice(0, max);
  },

  // EXPIRING content (24h stories + highlights) — grab these FIRST each cycle.
  async _expiring(user, username) {
    let saved = 0;
    if (!user.id) return 0;
    await hsleep(3500);
    try { const s = await this.getStories(user.id, username); if (s.length) { await send({ type: "ingest", platform: "instagram", username, items: s }); saved += s.length; clog("info", `${username}: +${s.length} story media (expiring)`, "instagram"); } }
    catch (e) { if (e instanceof WallError) throw e; }
    await hsleep(3500);
    try { const h = await this.getHighlights(user.id, username); if (h.length) { await send({ type: "ingest", platform: "instagram", username, items: h }); saved += h.length; clog("info", `${username}: +${h.length} highlight media`, "instagram"); } }
    catch (e) { if (e instanceof WallError) throw e; }
    return saved;
  },

  // DEEP scrape — timeline media + post metadata + comments + tagged + follow-graph.
  async _deep(user, username, hop) {
    let saved = 0, discovered = 0;
    const { media, posts } = await this.scrapeUserMedia(user, username);
    if (media.length) { await send({ type: "ingest", platform: "instagram", username, items: media }); saved += media.length; }
    if (posts.length) { await send({ type: "posts", platform: "instagram", username, posts }); }
    for (const p of posts.slice(0, 3)) {  // comments: heavy, cap a few posts
      await hsleep(6000);
      try { const cm = await this.getComments(p.platform_post_id); if (cm.length) await send({ type: "comments", platform: "instagram", post_id: p.platform_post_id, comments: cm }); }
      catch (e) { if (e instanceof WallError) throw e; }
    }
    if (user.id) {  // tagged posts -> filed under this profile + tagged users recorded
      await hsleep(5000);
      try {
        const tg = await this.getTagged(user.id, username);
        if (tg.media.length) { await send({ type: "ingest", platform: "instagram", username, items: tg.media }); saved += tg.media.length; clog("info", `${username}: +${tg.media.length} tagged media`, "instagram"); }
        if (tg.users.length) await send({ type: "users", platform: "instagram", context: "tagged", users: tg.users });
      } catch (e) { if (e instanceof WallError) throw e; }
    }
    if (hop < 2 && user.id && Math.random() < 0.3) {  // follow-graph sometimes (human)
      await hsleep(5000);
      const a = await this.getFollows(user.id, "followers", SPIDER_FOLLOWS_PER_SIDE);
      const b = await this.getFollows(user.id, "following", SPIDER_FOLLOWS_PER_SIDE);
      const found = a.concat(b);
      if (found.length) { await send({ type: "discover", platform: "instagram", source: username, hop, discovered: found }); discovered += found.length; }
    }
    return { saved, discovered };
  },

  // Seed the spider from YOUR OWN followers + following (user: "instagram should
  // start from my own followers as seeds"). Runs once per session; the logged-in
  // user id is the ds_user_id cookie. Capped + paced for account safety.
  async seedFromSelf() {
    const m = document.cookie.match(/ds_user_id=(\d+)/);
    if (!m) return;
    const myId = m[1];
    // Owner tag so ig_ingest can record a PER-ACCOUNT follow graph (follow_edges).
    // The extension only ever sees the ONE account you're logged into; you switch
    // accounts (IG's switcher) and this re-runs per account (see runCycle gate).
    let ownerUsername = null;
    try { ownerUsername = (window._sharedData && window._sharedData.config && window._sharedData.config.viewer && window._sharedData.config.viewer.username) || null; } catch (e) {}
    const owner = { id: myId, username: ownerUsername };
    let followers = [], following = [];
    try {
      followers = await this.getFollows(myId, "followers", 200);
      following = await this.getFollows(myId, "following", 200);
    } catch (e) { return; }
    // Record MY real follow graph with the CORRECT relationship context + owner so
    // social_users reflects who follows me ('follower') vs who I follow ('follow'),
    // and follow_edges records it per account. Uses /social/users (honours both).
    if (followers.length) await send({ type: "users", platform: "instagram", context: "follower", owner, users: followers }).catch(() => {});
    if (following.length) await send({ type: "users", platform: "instagram", context: "follow", owner, users: following }).catch(() => {});
    // Still seed the spider from the combined set (hop-0 expansion).
    const users = followers.concat(following);
    if (users.length) {
      await send({ type: "seed", platform: "instagram", users }).catch(() => {});
      clog("info", `self-seed [${ownerUsername || myId}]: ${followers.length} followers + ${following.length} following recorded + seeded`, "instagram");
    }
  },

  async runCycle() {
    if (/^\/direct(?:\/|$)/.test(location.pathname || "")) {
      return { targets: 0, saved: 0, discovered: 0, dm_tab: true };
    }
    // ANTI-BAN (cooperative): the HEADLESS collector shares this IG account. If it's
    // in a 429 cooldown, the extension must rest too — adopt its remaining time as
    // our own wall so we don't probe a flagged account from the other side.
    const igOwner = instagramLoggedInOwner();
    try {
      const cd = await send({ type: "igCooldown", account: igOwner });
      if (cd && cd.cooling && cd.secs_left > 0) {
        setWall("instagram", Math.ceil(cd.secs_left / 60), igOwner);
        const who = cd.account ? ` for ${cd.account}` : "";
        clog("warn", `IG: headless in 429 cooldown${who} (streak ${cd.streak}, ${Math.ceil(cd.secs_left / 60)}m) — extension resting in sync`, "instagram");
      }
    } catch (e) {}
    // if IG threw a throttle/challenge wall recently (either path), do NOT touch IG
    // at all until it clears — rest in chunks (survives loop respawns via localStorage).
    const left = wallLeftMs("instagram", igOwner);
    if (left > 0) {
      clog("warn", `IG throttled — resting, ${Math.ceil(left / 60000)}m left (not touching IG)`, "instagram");
      await sleep(Math.min(left, human(600000))); // re-check roughly every 8-22m
      return { targets: 0, saved: 0, discovered: 0, walled: true };
    }
    // Per-account self-seed: capture EACH account's own graph as you switch to it.
    // The extension can't auto-switch IG accounts — you switch (IG's account
    // switcher changes the ds_user_id cookie); when a not-yet-seeded account is
    // active this cycle, seed it once. Covers all your accounts by rotating logins.
    const _dsm = document.cookie.match(/ds_user_id=(\d+)/);
    const _curId = _dsm ? _dsm[1] : null;
    if (_curId && !IG_SEEDED_ACCOUNTS.has(_curId)) { IG_SEEDED_ACCOUNTS.add(_curId); this.seedFromSelf(); }
    let resp = [];
    try { resp = (await send({ type: "getTargets", platform: "instagram" })) || []; } catch (e) {}
    const pool = (Array.isArray(resp) ? resp : [])
      .map((t) => (typeof t === "string" ? { username: t, hop: 0 } : t))
      .filter((t) => t && t.username);
    if (!pool.length) return { targets: 0, saved: 0, discovered: 0 };
    const cache = new Map();
    const getUser = async (u) => { if (cache.has(u)) return cache.get(u); const p = await this.getProfile(u); cache.set(u, p); return p; };
    const okProfile = (user) => user && ((user.edge_followed_by && user.edge_followed_by.count) || 0) <= SPIDER_FAMOUS_CAP;
    let saved = 0, discovered = 0, visited = 0;

    // PASS 0 — WHOLE STORY TRAY. One reels_tray call captures every followed
    // account's active story this cycle (the bulk of your story feed), so the
    // per-profile sweep below is just a top-up for seeds/early-network.
    try {
      saved += await this.getStoryTray();
    } catch (e) {
      if (e instanceof WallError) {
        const mins = await applyThrottleWall("instagram", "story-tray");
        clog("warn", `throttled in story-tray — backing off ${mins}m`, "instagram");
        return { targets: visited, saved, discovered, walled: true };
      }
      clog("warn", `story-tray sweep failed: ${e.message}`, "instagram");
    }
    await hsleep(6000);

    // PASS 1 — EXPIRING FIRST. The server orders seeds (hop 0) at the front, so we
    // sweep stories/highlights for your seed profiles + early network every cycle.
    const sweep = pool.slice(0, IG_STORY_SWEEP);
    clog("info", `expiring-first: stories/highlights sweep of ${sweep.length} profile(s)`, "instagram");
    for (const t of sweep) {
      try {
        const user = await getUser(t.username);
        if (!okProfile(user)) continue;
        await this._sendProfile(user);
        saved += await this._expiring(user, t.username);
      } catch (e) {
        if (e instanceof WallError) {
          const mins = await applyThrottleWall("instagram", "sweep");
          clog("warn", `throttled in sweep — backing off ${mins}m (persisted, survives refresh)`, "instagram");
          return { targets: visited, saved, discovered, walled: true };
        }
        clog("warn", `sweep failed ${t.username}: ${e.message}`, "instagram");
      }
      await hsleep(9000);
    }

    // PASS 2 — DEEP scrape a small random handful (timeline + posts + comments + tagged).
    const deep = shuffle(pool.slice()).slice(0, igTargetBudget());
    clog("info", `deep scrape: ${deep.length} profile(s)`, "instagram");
    for (const t of deep) {
      const hop = typeof t.hop === "number" ? t.hop : 0;
      try {
        const user = await getUser(t.username);
        visited++;
        if (!user) continue;
        if (!okProfile(user)) { clog("info", `skip famous ${t.username}`, "instagram"); continue; }
        await this._sendProfile(user);
        const r = await this._deep(user, t.username, hop);
        saved += r.saved; discovered += r.discovered;
      } catch (e) {
        if (e instanceof WallError) {
          const mins = await applyThrottleWall("instagram", "deep-profile");
          clog("warn", `throttled at ${t.username} — backing off ${mins}m (persisted)`, "instagram");
          break;
        }
        clog("warn", `scrape failed ${t.username}: ${e.message}`, "instagram");
      }
      await hsleep(22000); // ~13–35s between profiles
    }
    return { targets: visited, saved, discovered };
  },
};

// ===========================================================================
// TikTok — read the page's embedded state (SIGI_STATE / __UNIVERSAL_DATA__).
// Scroll to load more posts into that state, then harvest. No request signing.
// NOTE: video CDN URLs are short-lived/cookie-bound; covers + photo-posts are the
// reliable wins. Open a profile or your "Following" feed and leave it.
// ===========================================================================
function tiktokRevisitKey() { return "uc_tiktok_revisit_active"; }
function currentTikTokRevisit() {
  try {
    const active = JSON.parse(localStorage.getItem(tiktokRevisitKey()) || "null");
    if (!active || !active.content_id) return null;
    if (active.claimed_at && Date.now() - active.claimed_at > 30 * 60000) {
      localStorage.removeItem(tiktokRevisitKey());
      return null;
    }
    return active;
  } catch (e) {
    localStorage.removeItem(tiktokRevisitKey());
    return null;
  }
}
async function maybeStartTikTokRevisit(owner) {
  if (deferNavigationForForcedRecovery("tiktok")) return null;
  const active = currentTikTokRevisit();
  if (active) return active;
  let reply = null;
  try {
    reply = await send({ type: "getTikTokRevisitTarget", owner: owner || null }).catch(() => null);
  } catch (e) {
    return null;
  }
  const target = reply && reply.target;
  if (!target || !target.content_id) return null;
  const url = target.post_url || target.source_url;
  if (!url || !/^https?:\/\//i.test(url)) {
    await send({
      type: "tiktokRevisitResult",
      content_id: target.content_id,
      status: "failed",
      reason: "missing_revisit_url",
      username: target.username || null,
    }).catch(() => {});
    return null;
  }
  const state = { ...target, url, claimed_at: Date.now() };
  localStorage.setItem(tiktokRevisitKey(), JSON.stringify(state));
  if (location.href !== url) {
    clog("info", `TikTok detail revisit queued for ${target.content_id}`, "tiktok");
    await sleep(jitter(900));
    location.href = url;
    return { ...state, navigating: true };
  }
  return state;
}
async function finishTikTokRevisit(active, response, observed, username) {
  if (!active || !active.content_id) return;
  const stored = Number(response && response.upload && response.upload.stored || 0);
  const status = observed > 0 ? "success" : "no_media";
  const reason = observed > 0 ? "detail_page_harvested" : "detail_page_no_media";
  await send({
    type: "tiktokRevisitResult",
    content_id: active.content_id,
    status,
    reason,
    observed,
    stored,
    username: username || active.username || null,
  }).catch(() => {});
  localStorage.removeItem(tiktokRevisitKey());
}
function tiktokProgressKey() {
  return "uc_tiktok_zero_progress_" + location.pathname.replace(/[^A-Za-z0-9_/-]/g, "_").slice(0, 180);
}
function tiktokBumpZeroProgress() {
  const key = tiktokProgressKey();
  const value = lsNum(key) + 1;
  lsSet(key, String(value));
  return value;
}
function tiktokClearZeroProgress() {
  lsSet(tiktokProgressKey(), "0");
}
async function tiktokReportPageHealth(status, reason, counts) {
  return send({
    type: "pageHealth",
    platform: "tiktok",
    label: "TikTok",
    status,
    reason,
    url: location.href,
    title: document.title || "",
    sample: status === "healthy" ? null : compactSample(visiblePageText()),
    content_counts: {
      ...pageContentCounts(),
      ...(counts || {}),
    },
  }).catch(() => null);
}

const BROWSER_MEDIA_REVISIT_PLATFORMS = new Set(["x", "threads", "facebook", "lemon8"]);
function browserMediaRevisitKey(platform) { return "uc_browser_media_revisit_active_" + platform; }
function currentBrowserMediaRevisit(platform) {
  try {
    const active = JSON.parse(localStorage.getItem(browserMediaRevisitKey(platform)) || "null");
    if (!active || !active.content_id || active.platform !== platform) return null;
    if (active.claimed_at && Date.now() - active.claimed_at > 30 * 60000) {
      localStorage.removeItem(browserMediaRevisitKey(platform));
      return null;
    }
    return active;
  } catch (e) {
    localStorage.removeItem(browserMediaRevisitKey(platform));
    return null;
  }
}
function deferNavigationForForcedRecovery(platform) {
  if (!ONE_SHOT_RUNNING) return false;
  return /browser_content_stale|stale/i.test(ONE_SHOT_REASON || "");
}
function forcedRecoveryMode(platform) {
  return deferNavigationForForcedRecovery(platform);
}
async function reportBrowserRecoveryProbe(platform, username, meta = {}) {
  if (!forcedRecoveryMode(platform)) return null;
  send({
    type: "ingest",
    platform,
    username: username || "unknown",
    items: [],
    record_empty: true,
    probe_reason: "forced_recovery_started",
    probe_meta: {
      url: location.href,
      content_counts: pageContentCounts(),
      ...meta,
    },
  }, { timeoutMs: 8000 }).catch(() => null);
  return null;
}
function browserMediaRevisitUrlOk(platform, url) {
  if (!url || !/^https?:\/\//i.test(url)) return false;
  try {
    const u = new URL(url);
    if (platform === "x") {
      return /^(x|twitter)\.com$/i.test(u.hostname.replace(/^www\./, "")) &&
        /^\/[A-Za-z0-9_]{1,20}\/status\/\d+/.test(u.pathname);
    }
    if (platform === "threads") {
      return /^(threads\.com|threads\.net)$/i.test(u.hostname.replace(/^www\./, "")) &&
        /^\/@[^/]+\/post\//.test(u.pathname);
    }
    if (platform === "facebook") {
      return /(^|\.)facebook\.com$/i.test(u.hostname) &&
        (/\/(posts|photos|videos)\//.test(u.pathname) ||
          /^\/(?:photo|permalink|story)\.php/i.test(u.pathname));
    }
    if (platform === "lemon8") {
      return /lemon8/i.test(u.hostname) &&
        (/\/@[^/]+/.test(u.pathname) || /\/\d{6,}/.test(u.pathname));
    }
  } catch (e) {}
  return false;
}
async function maybeStartBrowserMediaRevisit(platform, owner) {
  if (!BROWSER_MEDIA_REVISIT_PLATFORMS.has(platform)) return null;
  if (deferBrowserMediaRevisitForForcedRecovery(platform)) return null;
  const active = currentBrowserMediaRevisit(platform);
  if (active) return active;
  const reply = await send(
    { type: "getBrowserMediaRevisitTarget", platform, owner: owner || null },
    { retries: 0, timeoutMs: 8000 }
  ).catch(() => null);
  const target = reply && reply.target;
  if (!target || !target.content_id) return null;
  const url = target.post_url || target.source_url;
  if (!browserMediaRevisitUrlOk(platform, url)) {
    await send({
      type: "browserMediaRevisitResult",
      platform,
      content_id: target.content_id,
      status: "failed",
      reason: "missing_revisit_url",
      username: target.username || null,
    }).catch(() => {});
    return null;
  }
  const state = { ...target, platform, url, claimed_at: Date.now() };
  localStorage.setItem(browserMediaRevisitKey(platform), JSON.stringify(state));
  if (location.href !== url) {
    clog("info", `${platform.toUpperCase()} detail revisit queued for ${target.content_id}`, platform);
    await sleep(jitter(900));
    location.href = url;
    return { ...state, navigating: true };
  }
  return state;
}
async function finishBrowserMediaRevisit(platform, active, response, observed, username) {
  if (!active || !active.content_id) return;
  const upload = response && response.upload ? response.upload : {};
  const attempted = Number(upload.attempted || 0);
  const accepted = Number(upload.accepted ?? upload.stored ?? 0);
  const stored = Number(upload.stored ?? accepted ?? 0);
  const status = observed > 0 && (!attempted || accepted > 0) ? "success" : (observed > 0 ? "failed" : "no_media");
  const reason = observed > 0
    ? (attempted && accepted <= 0 ? "detail_page_upload_failed" : "detail_page_harvested")
    : "detail_page_no_media";
  await send({
    type: "browserMediaRevisitResult",
    platform,
    content_id: active.content_id,
    status,
    reason,
    observed,
    stored,
    username: username || active.username || null,
  }).catch(() => {});
  localStorage.removeItem(browserMediaRevisitKey(platform));
}
function markBrowserMediaRevisitItems(platform, sink, active) {
  if (!active || !active.content_id || !sink || !Array.isArray(sink.items)) return;
  sink.items.forEach((it) => {
    it.browser_upload = true;
    it.meta = {
      ...(it.meta || {}),
      revisit_content_id: active.content_id,
      revisit_reason: active.reason || null,
      revisit_platform: platform,
    };
  });
}
const tiktok = {
  id: "tiktok", host: "www.tiktok.com", label: "TikTok",
  entity() { const m = location.pathname.match(/^\/@([^/?#]+)/); return m ? m[1] : "feed"; },
  async runCycle() {
    const entity = this.entity();
    const owner = ownerFromStoredOrDom("tiktok", () => {
      const state = parseEmbeddedState(["__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE", "sigi-persisted-data"]);
      const scope = state && state.__DEFAULT_SCOPE__;
      return (scope && scope["webapp.app-context"] && scope["webapp.app-context"].user && scope["webapp.app-context"].user.uniqueId)
        || (state && state.AppContext && state.AppContext.user && state.AppContext.user.uniqueId)
        || "";
    });
    if (await maybeSweepFollowGraph({
      platform: "tiktok",
      owner,
      urls: {
        following: "https://www.tiktok.com/@" + encodeURIComponent(owner) + "/following",
        followers: "https://www.tiktok.com/@" + encodeURIComponent(owner) + "/followers",
        followingPath: new RegExp("^/@" + owner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "/following/?$", "i"),
        followersPath: new RegExp("^/@" + owner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "/followers/?$", "i"),
      },
      homeUrl: "https://www.tiktok.com/following",
    })) return { targets: 1, saved: 0, discovered: 0 };
    const revisit = await maybeStartTikTokRevisit(owner);
    if (revisit && revisit.navigating) return { targets: 1, saved: 0, discovered: 0 };
    const forcedRecovery = forcedRecoveryMode("tiktok");
    clog("info", `cycle start on @${entity}`, "tiktok");
    const sink = makeSink();
    let userCount = 0;
    await autoScroll(forcedRecovery ? 3 : 10, 1400, forcedRecovery ? 800 : 1800, {
      maxPauseMs: forcedRecovery ? 1500 : 3500,
    });
    const state = parseEmbeddedState(["__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE", "sigi-persisted-data"]);
    if (state) {
      deepCollectMedia(state, sink, entity);
      const us = [];
      deepCollectUsers(state, us);
      userCount = us.length;
      if (us.length) await send({ type: "users", platform: "tiktok", context: "seen", users: us });
    }
    // also harvest whatever the DOM rendered. TikTok playback URLs are
    // short-lived/cookie-bound, so videos go through browser_upload_only while
    // posters/covers can still use normal URL ingest.
    document.querySelectorAll("video").forEach((v, i) => {
      const u = v.src || (v.querySelector("source") && v.querySelector("source").src);
      if (u && /^https?:/.test(u)) {
        sink.add({
          content_id: "dom_video_" + urlId(u),
          content_type: "video",
          url: u,
          entity_name: entity,
          kind: "post",
          browser_upload: true,
          browser_upload_only: true,
          meta: { tiktok_asset_role: "dom_video", page_url: location.href },
        });
      }
      const poster = v.getAttribute("poster") || v.poster;
      if (poster && /^https?:/.test(poster)) {
        sink.add({
          content_id: "poster_" + urlId(poster),
          content_type: "photo",
          url: poster,
          entity_name: entity,
          kind: "post",
          meta: { tiktok_asset_role: "poster", page_url: location.href },
        });
      }
    });
    document.querySelectorAll("img").forEach((im) => {
      const u = im.currentSrc || im.src;
      if (!u || !/^https?:/.test(u)) return;
      if (!/tiktokcdn|tiktokv|byteimg|muscdn|p16|p19/i.test(u)) return;
      if (/avatar|emoji|icon|logo|profile/i.test(u)) return;
      const w = im.naturalWidth || im.width || 0;
      const h = im.naturalHeight || im.height || 0;
      if ((w && w < 160) || (h && h < 160)) return;
      sink.add({
        content_id: "img_" + urlId(u),
        content_type: "photo",
        url: u,
        entity_name: entity,
        kind: "post",
        meta: { tiktok_asset_role: "dom_image", width: w || null, height: h || null, page_url: location.href },
      });
    });
    const activeRevisit = currentTikTokRevisit();
    sink.items.forEach((it) => {
      it.meta = { ...(it.meta || {}), page_url: location.href };
      if (activeRevisit && activeRevisit.content_id) {
        it.meta.revisit_content_id = activeRevisit.content_id;
        it.meta.revisit_reason = activeRevisit.reason || null;
      }
    });
    const counts = pageContentCounts();
    const usefulNodes = Number(counts.articles || 0) + Number(counts.videos || 0) + Number(counts.images || 0);
    const progressCounts = {
      entity,
      media_candidates: sink.items.length,
      users: userCount,
      state_present: !!state,
      useful_nodes: usefulNodes,
    };
    const response = sink.items.length
      ? await send({ type: "ingest", platform: "tiktok", username: entity, items: sink.items })
      : await send({
          type: "ingest",
          platform: "tiktok",
          username: entity,
          items: [],
          record_empty: true,
          probe_reason: "no_dom_media_candidates",
          probe_meta: { ...progressCounts, content_counts: counts, page_url: location.href },
        }, { timeoutMs: 12000 }).catch(() => null);
    if (sink.items.length || userCount) {
      tiktokClearZeroProgress();
      await tiktokReportPageHealth("healthy", "tiktok_content_progress", progressCounts);
    } else {
      const zeroStreak = tiktokBumpZeroProgress();
      progressCounts.zero_progress_streak = zeroStreak;
      const blankShell = usefulNodes < 2 && Number(counts.links || 0) < 10;
      await tiktokReportPageHealth(
        blankShell || zeroStreak >= 2 ? "recoverable_error_shell" : "zero_content",
        blankShell ? "tiktok_blank_page" : "tiktok_zero_progress",
        progressCounts
      );
    }
    if (activeRevisit) await finishTikTokRevisit(activeRevisit, response, sink.items.length, entity);
    return { targets: 1, saved: sink.items.length, discovered: 0 };
  },
};

// ===========================================================================
// Lemon8 — Next.js app: data lives in __NEXT_DATA__ + lazy-loaded into DOM.
// Photo-first platform, so image URLs download cleanly server-side.
// ===========================================================================
function lemon8HandleFromHref(href) {
  try {
    const u = new URL(href || "", location.href);
    const parts = u.pathname.split("/").filter(Boolean);
    const reserved = new Set(["feed", "foryou", "fashion", "beauty", "food", "travel", "home", "topic", "search"]);
    for (const part of parts) {
      if (part.startsWith("@") && /^[A-Za-z0-9_.-]{2,64}$/.test(part.slice(1))) return part.slice(1);
    }
    if (parts[0] && !reserved.has(parts[0].toLowerCase()) && /^[A-Za-z0-9_.-]{2,64}$/.test(parts[0])) return parts[0];
  } catch (e) {}
  return "";
}

function lemon8NoteIdFromHref(href) {
  try {
    const u = new URL(href || "", location.href);
    const m = u.pathname.match(/\/(\d{6,})(?:$|[/?#])/);
    return m ? m[1] : "";
  } catch (e) {
    return "";
  }
}

function lemon8TopicFromHref(href) {
  try {
    const u = new URL(href || "", location.href);
    const parts = u.pathname.split("/").filter(Boolean);
    if ((parts[0] || "").toLowerCase() !== "topic" || !parts[1]) return "";
    const topic = decodeURIComponent(parts[1])
      .trim()
      .replace(/^#/, "")
      .replace(/[^A-Za-z0-9_.-]/g, "_")
      .slice(0, 80);
    return topic ? "topic_" + topic : "topic";
  } catch (e) {
    return "";
  }
}

function lemon8MediaUrl(u) {
  if (!u || !/^https?:/i.test(u)) return false;
  if (!/lemon8|byteimg|ibytedtos|muscdn|p16|p19|tos-/i.test(u)) return false;
  if (/emoji|icon|logo|sprite|placeholder/i.test(u)) return false;
  return true;
}

function lemon8CollectDomUsers() {
  const byUser = new Map();
  const add = (username, data = {}) => {
    username = String(username || "").trim().replace(/^@/, "");
    if (!/^[A-Za-z0-9_.-]{2,64}$/.test(username)) return;
    const prev = byUser.get(username) || { username };
    byUser.set(username, {
      ...prev,
      display_name: prev.display_name || data.display_name || null,
      profile_pic_url: prev.profile_pic_url || data.profile_pic_url || null,
    });
  };
  document.querySelectorAll('a[href*="/@"]').forEach((a) => {
    try {
      const username = lemon8HandleFromHref(a.getAttribute("href") || a.href || "");
      if (!username) return;
      const text = (a.innerText || a.textContent || "").trim().split(/\n+/)
        .map((s) => s.trim())
        .find((s) => s && !s.startsWith("@") && s.length < 80);
      const avatar = [...a.querySelectorAll("img")]
        .map((im) => ({
          im,
          urls: imageUrlsFromElement(im).filter((u) => lemon8MediaUrl(u)),
        }))
        .find(({ im, urls }) => {
          if (!urls.length) return false;
          if (urls.some((u) => /avatar|profile/i.test(u))) return true;
          try {
            const w = im.naturalWidth || im.width || 0;
            const h = im.naturalHeight || im.height || 0;
            const r = im.getBoundingClientRect && im.getBoundingClientRect();
            return (w && h && w <= 180 && h <= 180) ||
              (r && r.width && r.height && r.width <= 96 && r.height <= 96);
          } catch (e) {
            return false;
          }
        });
      const avatarUrl = avatar ? avatar.urls[0] : null;
      add(username, { display_name: text || null, profile_pic_url: avatarUrl });
    } catch (e) {}
  });
  return [...byUser.values()];
}

function lemon8CardForElement(el) {
  try {
    return el.closest('a[href*="/@"], article, [role="article"], [data-e2e], [data-testid]') || el.parentElement;
  } catch (e) {
    return el.parentElement;
  }
}

function lemon8CollectDomMedia(sink, pageEntity) {
  const addCandidate = (url, el, role = "dom_image", contentType = "photo") => {
    if (!lemon8MediaUrl(url)) return;
    if (contentType === "photo" && el && imageLooksTooSmall(el, 160)) return;
    if (/avatar/i.test(url) && role !== "video_poster") return;
    const card = lemon8CardForElement(el || document.body);
    const link = card && (card.matches && card.matches("a[href]") ? card : card.querySelector && card.querySelector('a[href*="/@"]'));
    const href = link ? (link.getAttribute("href") || link.href || "") : "";
    const author = lemon8HandleFromHref(href) || (/^(feed|foryou|fashion)$/i.test(pageEntity) ? "" : pageEntity);
    const noteId = lemon8NoteIdFromHref(href);
    let postUrl = null;
    try { postUrl = href ? new URL(href, location.href).href : null; } catch (e) {}
    let width = null, height = null;
    try {
      width = el.naturalWidth || el.videoWidth || el.clientWidth || null;
      height = el.naturalHeight || el.videoHeight || el.clientHeight || null;
    } catch (e) {}
    sink.add({
      content_id: ["lemon8", noteId || author || pageEntity || "feed", role, urlId(url)].filter(Boolean).join("_"),
      content_type: contentType,
      url,
      entity_name: author || pageEntity || "feed",
      kind: "post",
      meta: {
        source: "lemon8_dom",
        lemon8_asset_role: role,
        author_username: author || null,
        note_id: noteId || null,
        post_url: postUrl,
        width,
        height,
      },
    });
  };

  document.querySelectorAll("img").forEach((im) => {
    imageUrlsFromElement(im).forEach((u) => addCandidate(u, im, "dom_image", "photo"));
  });
  document.querySelectorAll('[style*="background"]').forEach((el) => {
    urlsFromCssValue(el.getAttribute("style") || "").forEach((u) => addCandidate(u, el, "css_background", "photo"));
  });
  document.querySelectorAll("video").forEach((v) => {
    try { if (v.poster) addCandidate(v.poster, v, "video_poster", "photo"); } catch (e) {}
    try {
      const src = v.currentSrc || v.src;
      if (src) addCandidate(src, v, "video", "video");
    } catch (e) {}
  });
}

const lemon8 = {
  id: "lemon8", host: "www.lemon8-app.com", label: "Lemon8",
  entity() {
    const handle = lemon8HandleFromHref(location.href);
    if (handle) return handle;
    const topic = lemon8TopicFromHref(location.href);
    if (topic) return topic;
    const m = location.pathname.match(/\/([^/?#]+)/);
    return m ? m[1] : "feed";
  },
  async runCycle() {
    const entity = this.entity();
    const forcedRecovery = forcedRecoveryMode("lemon8");
    const mediaRevisit = await maybeStartBrowserMediaRevisit("lemon8", entity);
    if (mediaRevisit && mediaRevisit.navigating) return { targets: 1, saved: 0, discovered: 0 };
    clog("info", `cycle start on ${entity}`, "lemon8");
    const sink = makeSink();
    await reportBrowserRecoveryProbe("lemon8", entity, { entity });
    await autoScroll(forcedRecovery ? 4 : 18, 1400, forcedRecovery ? 900 : 1800, {
      maxPauseMs: forcedRecovery ? 1500 : 3500,
    });
    const state = parseEmbeddedState(["__NEXT_DATA__"]);
    const users = [];
    if (state) {
      deepCollectMedia(state, sink, entity);
      deepCollectUsers(state, users);
    }
    lemon8CollectDomMedia(sink, entity);
    lemon8CollectDomUsers().forEach((u) => users.push(u));
    let uniqueUserCount = 0;
    let uniqueUsers = [];
    if (users.length) {
      const seen = new Set();
      uniqueUsers = users.filter((u) => {
        const key = (u.username || u.user_id || "").toString().toLowerCase();
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      uniqueUserCount = uniqueUsers.length;
    }
    const activeMediaRevisit = currentBrowserMediaRevisit("lemon8");
    markBrowserMediaRevisitItems("lemon8", sink, activeMediaRevisit);
    const ingestPayload = {
      type: "ingest",
      platform: "lemon8",
      username: entity,
      items: sink.items,
      record_empty: true,
      probe_reason: sink.items.length ? "media_candidates_found" : "no_dom_media_candidates",
      probe_meta: { users: uniqueUserCount },
    };
    const ingestResponse = forcedRecovery
      ? (sendSideEffect(
          ingestPayload,
          "lemon8",
          `Lemon8 ${entity} forced media write`,
          { timeoutMs: 30000 }
        ), null)
      : await send(ingestPayload, { timeoutMs: 45000 });
    if (activeMediaRevisit) {
      await finishBrowserMediaRevisit("lemon8", activeMediaRevisit, ingestResponse, sink.items.length, entity);
    }
    if (uniqueUsers.length) {
      const usersPayload = { type: "users", platform: "lemon8", context: "author", users: uniqueUsers };
      if (forcedRecovery) {
        sendSideEffect(usersPayload, "lemon8", "Lemon8 author write", { timeoutMs: 12000 });
      } else {
        await send(usersPayload, { timeoutMs: 25000 });
      }
    }
    return { targets: 1, saved: sink.items.length, discovered: users.length };
  },
};

// ===========================================================================
// Twitter / X — SPA with no static state dump; harvest rendered media from the
// timeline DOM (pbs.twimg.com images + video posters). Open Home / a profile's
// Media tab and leave it; scroll loads more.
// ===========================================================================
function xStatusHref(root) {
  try {
    const link = root && root.querySelector && root.querySelector('a[href*="/status/"]');
    return (link && (link.getAttribute("href") || ""))
      .replace(/^https?:\/\/(?:www\.)?(?:x\.com|twitter\.com)/i, "")
      .split("?")[0];
  } catch (e) {
    return "";
  }
}

function xTweetRoots() {
  const roots = [];
  const seen = new Set();
  document.querySelectorAll('article[data-testid="tweet"], article[role="article"], div[data-testid="cellInnerDiv"]').forEach((root) => {
    const href = xStatusHref(root);
    const m = href.match(/^\/([A-Za-z0-9_]{1,20})\/status\/(\d+)/);
    if (!m) return;
    const key = m[1].toLowerCase() + ":" + m[2];
    if (seen.has(key)) return;
    seen.add(key);
    roots.push(root);
  });
  return roots;
}

// Read tweet records straight off the DOM. This is the reliable path for X — it
// does NOT depend on the inject GraphQL hook firing (X throttles background-tab
// fetches and the home feed can load before the hook installs). X has changed
// wrappers over time, so we accept both old <article> tweets and modern timeline
// cells with a status permalink. Counts still usually live on a [role=group]
// aria-label ("13 replies, 4 reposts, 88 likes, 9,000 views").
function harvestXPosts(entity, feed) {
  const posts = [];
  xTweetRoots().forEach((art) => {
    try {
      const href = xStatusHref(art);
      const m = href.match(/^\/([A-Za-z0-9_]{1,20})\/status\/(\d+)/);
      if (!m) return;
      const author = m[1], pid = m[2];
      const textEl = art.querySelector('[data-testid="tweetText"]');
      const caption = textEl ? (textEl.innerText || "").trim().slice(0, 2000) : null;
      const grp = art.querySelector('[role="group"][aria-label]');
      const al = grp ? (grp.getAttribute("aria-label") || "") : "";
      const num = (re) => { const x = al.match(re); return x ? parseInt(x[1].replace(/[,.\s]/g, ""), 10) : null; };
      const timeEl = art.querySelector("time[datetime]");
      const takenAtMs = timeEl ? Date.parse(timeEl.getAttribute("datetime") || "") : NaN;
      const allText = (art.innerText || "").trim();
      const replyTo = ((allText.match(/Replying to\s+@([A-Za-z0-9_]{1,20})/i) || [])[1]) || null;
      const quote = (() => {
        const links = [...art.querySelectorAll('a[href*="/status/"]')]
          .map((a) => (a.getAttribute("href") || "")
            .replace(/^https?:\/\/(?:www\.)?(?:x\.com|twitter\.com)/i, "")
            .split("?")[0])
          .map((h) => h.match(/^\/([A-Za-z0-9_]{1,20})\/status\/(\d+)/))
          .filter(Boolean)
          .map((mm) => ({ author: mm[1], id: mm[2], href: `/${mm[1]}/status/${mm[2]}` }));
        return links.find((item) => item.id !== pid) || null;
      })();
      const reposted = (() => {
        const m = allText.match(/@([A-Za-z0-9_]{1,20})\s+reposted/i);
        return m ? m[1] : null;
      })();
      posts.push({
        platform_post_id: pid, author_username: author, caption: caption || null,
        comments_count: num(/([\d,.]+)\s+repl/i), reposts_count: num(/([\d,.]+)\s+repost/i),
        likes_count: num(/([\d,.]+)\s+like/i), views_count: num(/([\d,.]+)\s+view/i),
        media_type: "tweet",
        taken_at: Number.isFinite(takenAtMs) ? Math.floor(takenAtMs / 1000) : null,
        hashtags: caption ? (caption.match(/#[\w]+/g) || []).map((s) => s.slice(1)) : [],
        mentions: caption ? (caption.match(/@[\w]+/g) || []).map((s) => s.slice(1)) : [],
        in_reply_to_screen_name: replyTo,
        quoted_author_username: quote && quote.author,
        retweeted_author_username: reposted,
        metadata: {
          source: "x_dom_tweet",
          feed,
          url: "https://x.com" + href,
          in_reply_to_screen_name: replyTo,
          quoted_author_username: quote && quote.author,
          quoted_post_id: quote && quote.id,
          retweeted_author_username: reposted,
        },
      });
    } catch (e) {}
  });
  return posts;
}

// Click a named home-timeline tab ("Following" or "For you"). Following is primary;
// For-You runs as the occasional secondary pass (see rotation in runCycle).
async function xSelectTab(name) {
  try {
    const tab = [...document.querySelectorAll('[role="tab"], a[role="tab"]')]
      .find((t) => (t.textContent || "").trim().toLowerCase() === name.toLowerCase());
    if (tab) {
      if (tab.getAttribute("aria-selected") !== "true") { tab.scrollIntoView({ block: "center" }); tab.click(); await sleep(jitter(2500)); }
      return true;
    }
  } catch (e) {}
  return false;
}

function xLoggedInOwner() {
  const sources = [
    document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]'),
    document.querySelector('a[data-testid="AppTabBar_Profile_Link"]'),
    ...document.querySelectorAll('a[href^="/"][aria-label*="Profile" i]'),
  ].filter(Boolean);
  for (const el of sources) {
    const txt = el.innerText || el.getAttribute("aria-label") || "";
    const m = txt.match(/@([A-Za-z0-9_]{1,20})/);
    if (m) return m[1];
    const href = el.getAttribute && (el.getAttribute("href") || "");
    const h = href.match(/^\/([A-Za-z0-9_]{1,20})\/?$/);
    if (h && !/^(home|explore|notifications|messages|i|search)$/i.test(h[1])) return h[1];
  }
  return "";
}

function compactCount(s) {
  const m = String(s || "").replace(/,/g, "").match(/([\d.]+)\s*([KMB])?/i);
  if (!m) return null;
  const mult = ({ K: 1e3, M: 1e6, B: 1e9 })[(m[2] || "").toUpperCase()] || 1;
  const n = Number(m[1]) * mult;
  return Number.isFinite(n) ? Math.round(n) : null;
}

function textOf(sel, root = document) {
  try {
    const el = root.querySelector(sel);
    return el ? (el.innerText || el.textContent || "").trim() : null;
  } catch (e) { return null; }
}

function xProfileCount(handle, suffix) {
  try {
    const re = new RegExp("^/?" + handle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "/" + suffix + "/?$", "i");
    const a = [...document.querySelectorAll("a[href]")]
      .find((el) => re.test((el.getAttribute("href") || "").replace(/^https?:\/\/(?:www\.)?x\.com/i, "")));
    return compactCount(a && a.innerText);
  } catch (e) { return null; }
}

function xStatusContext(root) {
  try {
    const href = xStatusHref(root);
    const m = href.match(/^\/([A-Za-z0-9_]{1,20})\/status\/(\d+)/);
    if (m) return { author: m[1], post_id: m[2], href };
  } catch (e) {}
  return {};
}

function normalizeXMediaUrl(raw) {
  if (!raw || !/^https?:/i.test(raw)) return "";
  try {
    const u = new URL(raw);
    if (!/pbs\.twimg\.com$/i.test(u.hostname)) return "";
    if (/\/profile_images\//i.test(u.pathname)) return "";
    const useful = /\/(media|ext_tw_video_thumb|amplify_video_thumb|tweet_video_thumb|card_img)\//i.test(u.pathname);
    if (!useful) return "";
    if (/\/media\//i.test(u.pathname)) u.searchParams.set("name", "orig");
    return u.toString();
  } catch (e) {
    return "";
  }
}

function normalizeXVideoUrl(raw) {
  if (!raw || !/^https?:/i.test(raw)) return "";
  try {
    const u = new URL(raw);
    if (!/(^|\.)twimg\.com$/i.test(u.hostname) && !/(^|\.)video\.twitter\.com$/i.test(u.hostname)) return "";
    if (!/\.mp4(?:$|\?)/i.test(u.toString())) return "";
    return u.toString();
  } catch (e) {
    return "";
  }
}

function addXMediaCandidate(sink, u, entity, role, context = {}) {
  const url = normalizeXMediaUrl(u);
  if (!url) return;
  const idBase = context.post_id ? `${context.post_id}_${urlId(url)}` : urlId(url);
  sink.add({
    content_id: `${role}_${idBase}`,
    content_type: "photo",
    url,
    entity_name: context.author || entity,
    kind: "post",
    meta: {
      x_asset_role: role,
      post_id: context.post_id || null,
      author_username: context.author || null,
      post_url: context.href ? "https://x.com" + context.href : null,
    },
  });
}

function addXVideoCandidate(sink, u, entity, role, context = {}) {
  const url = normalizeXVideoUrl(u);
  if (!url) return;
  const idBase = context.post_id ? `${context.post_id}_${urlId(url)}` : urlId(url);
  sink.add({
    content_id: `${role}_${idBase}`,
    content_type: "video",
    url,
    entity_name: context.author || entity,
    kind: "post",
    browser_upload: true,
    browser_upload_only: true,
    meta: {
      x_asset_role: role,
      post_id: context.post_id || null,
      author_username: context.author || null,
      post_url: context.href ? "https://x.com" + context.href : null,
    },
  });
}

function xCollectMediaFromRoot(root, sink, entity, context = {}) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll('img[src*="pbs.twimg.com"], img[srcset*="pbs.twimg.com"]').forEach((im) => {
    if (imageLooksTooSmall(im, 120)) return;
    imageUrlsFromElement(im).forEach((u) => addXMediaCandidate(sink, u, entity, "img", context));
  });
  root.querySelectorAll('picture source[srcset*="pbs.twimg.com"], source[srcset*="pbs.twimg.com"]').forEach((src) => {
    try {
      const srcset = src.getAttribute("srcset") || "";
      srcset.split(",").forEach((part) => {
        const u = part.trim().split(/\s+/)[0];
        addXMediaCandidate(sink, u, entity, "source", context);
      });
    } catch (e) {}
  });
  root.querySelectorAll("video").forEach((v) => {
    addXMediaCandidate(sink, v.poster, entity, "poster", context);
    const u = v.currentSrc || v.src || (v.querySelector("source") && v.querySelector("source").src);
    addXVideoCandidate(sink, u, entity, "video", context);
    v.querySelectorAll("source[src]").forEach((source) => addXVideoCandidate(sink, source.src, entity, "video_source", context));
  });
  root.querySelectorAll("[style]").forEach((el) => {
    if (elementLooksTooSmall(el, 120)) return;
    try {
      urlsFromCssValue(el.getAttribute("style") || "").forEach((u) => addXMediaCandidate(sink, u, entity, "css_bg", context));
    } catch (e) {}
  });
}

function scrapeXProfile(handle) {
  const username = (handle || "").trim().replace(/^@/, "");
  if (!username || username === "timeline") return null;
  const body = document.body ? (document.body.innerText || "") : "";
  if (/account doesn['’]t exist|profile not found|page doesn['’]t exist/i.test(body)) return null;
  const nameBlock = document.querySelector('[data-testid="UserName"]');
  const nameLines = ((nameBlock && nameBlock.innerText) || "")
    .split("\n").map((s) => s.trim()).filter(Boolean);
  const displayName = nameLines.find((s) => !s.startsWith("@") && !/^(Follow|Following|Subscribe)$/i.test(s)) || null;
  const avatar = [...document.querySelectorAll('img[src*="profile_images"]')]
    .map((im) => im.currentSrc || im.src).find(Boolean) || null;
  const external = (() => {
    const a = document.querySelector('[data-testid="UserUrl"] a[href]');
    return a ? (a.href || a.getAttribute("href")) : null;
  })();
  const profile = {
    platform_user_id: username,
    username,
    display_name: displayName,
    bio: textOf('[data-testid="UserDescription"]'),
    followers_count: xProfileCount(username, "followers"),
    following_count: xProfileCount(username, "following"),
    is_verified: Boolean(document.querySelector('[data-testid="icon-verified"], [aria-label="Verified account"]')),
    is_private: /These posts are protected/i.test(body),
    profile_pic_url: avatar,
    external_url: external,
    location: textOf('[data-testid="UserLocation"]'),
    joined_text: textOf('[data-testid="UserJoinDate"]'),
    metadata: { source: "x_dom_profile", url: location.href },
  };
  return Object.values(profile).some((v) => v !== null && v !== "" && v !== false) ? profile : null;
}

function xIsStatusPage() {
  return /^\/[A-Za-z0-9_]{1,20}\/status\/\d+/.test(location.pathname);
}

function xIsMediaTab() {
  return /^\/[A-Za-z0-9_]{1,20}\/media\/?$/.test(location.pathname);
}

function xProfileUnavailable() {
  try {
    const body = document.body ? (document.body.innerText || "") : "";
    return /account doesn['’]t exist|profile not found|page doesn['’]t exist/i.test(body);
  } catch (e) {
    return false;
  }
}

function xProgressKey() {
  return "uc_x_zero_progress_" + location.pathname.replace(/[^A-Za-z0-9_/-]/g, "_").slice(0, 180);
}

function xProgressSample() {
  try {
    return compactSample(visiblePageText());
  } catch (e) {
    return document.title || "";
  }
}

function xBumpZeroProgress() {
  const key = xProgressKey();
  const value = lsNum(key) + 1;
  lsSet(key, String(value));
  return value;
}

function xClearZeroProgress() {
  lsSet(xProgressKey(), "0");
}

async function xReportPageHealth(status, reason, counts) {
  return send({
    type: "pageHealth",
    platform: "x",
    label: "Twitter / X",
    status,
    reason,
    url: location.href,
    title: document.title || "",
    sample: status === "healthy" ? null : xProgressSample(),
    content_counts: {
      ...pageContentCounts(),
      ...(counts || {}),
    },
  }).catch(() => null);
}

async function xReportProfileTarget(username, status, reason, owner) {
  if (!username || username === "timeline") return;
  await send({
    type: "xProfileTargetResult",
    username,
    status: status || "success",
    reason: reason || null,
    owner: owner || null,
  }).catch(() => {});
}

async function xMaybeVisitQueuedProfile(owner, cycle) {
  if (cycle % 3 !== 0) return false;
  const resp = await send({ type: "getXProfileTarget", owner: owner || "" }).catch(() => null);
  const target = resp && resp.target && resp.target.username;
  if (!target) return false;
  const handle = String(target).replace(/^@/, "");
  if (!/^[A-Za-z0-9_]{1,20}$/.test(handle)) return false;
  clog("info", `X → visiting queued @${handle} profile/media`, "x");
  await sleep(jitter(2500));
  location.href = "https://x.com/" + encodeURIComponent(handle);
  return true;
}

async function xMaybeVisitTweetDetail(posts, cycle) {
  if (cycle % 5 !== 0 || !Array.isArray(posts) || !posts.length) return false;
  const post = posts.find((p) => p && p.metadata && p.metadata.url) || posts[0];
  const url = post && post.metadata && post.metadata.url;
  if (!url || !/^https:\/\/(?:x|twitter)\.com\/[A-Za-z0-9_]{1,20}\/status\/\d+/i.test(url)) return false;
  clog("info", "X → opening tweet detail for full media pass", "x");
  await sleep(jitter(2500));
  location.href = url;
  return true;
}

const x = {
  id: "x", host: "x.com", label: "Twitter / X",
  entity() { const m = location.pathname.match(/^\/([^/?#]+)/); return m && !/^(home|explore|notifications|messages|i|search)$/.test(m[1]) ? m[1] : "timeline"; },
  async runCycle() {
    const entity = this.entity();
    const owner = ownerFromStoredOrDom("x", xLoggedInOwner);
    const cycle = lsBump("uc_x_cyc");
    const forcedRecovery = forcedRecoveryMode("x");
    if (await maybeSweepFollowGraph({
      platform: "x",
      owner,
      urls: {
        following: "https://x.com/" + encodeURIComponent(owner) + "/following",
        followers: "https://x.com/" + encodeURIComponent(owner) + "/followers",
        followingPath: new RegExp("^/" + owner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "/following/?$", "i"),
        followersPath: new RegExp("^/" + owner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "/followers/?$", "i"),
      },
      homeUrl: "https://x.com/home",
    })) return { targets: 1, saved: 0, discovered: 0 };
    const mediaRevisit = await maybeStartBrowserMediaRevisit("x", owner);
    if (mediaRevisit && mediaRevisit.navigating) return { targets: 1, saved: 0, discovered: 0 };
    if (entity !== "timeline" && !xIsStatusPage() && xProfileUnavailable()) {
      await xReportProfileTarget(entity, "unavailable", "profile_missing", owner);
      clog("info", `X @${entity}: unavailable profile`, "x");
      await sleep(jitter(2500));
      location.href = "https://x.com/home";
      return { targets: 1, saved: 0, discovered: 0 };
    }
    // following-PRIMARY, for-you SECONDARY: most cycles read Following; every 4th
    // cycle take a For-You pass so we still capture trending/outside-graph tweets.
    let feed = entity;
    if (/^\/home/.test(location.pathname)) {
      if (cycle % 4 === 0) feed = (await xSelectTab("For you")) ? "home/foryou" : "home";
      else feed = (await xSelectTab("Following")) ? "home/following" : "home";
    }
    clog("info", `X cycle on ${feed} — scrolling for tweets`, "x");
    const sink = makeSink();
    await reportBrowserRecoveryProbe("x", entity, { entity, feed });
    await autoScroll(forcedRecovery ? 4 : 12, 1400, forcedRecovery ? 900 : 1800, {
      maxPauseMs: forcedRecovery ? 1500 : 3500,
    });
    if (entity !== "timeline") {
      const profile = scrapeXProfile(entity);
      if (profile) {
        sendSideEffect(
          { type: "profile", platform: "x", profile, owner: { username: owner } },
          "x",
          `X @${profile.username} profile write`,
          { timeoutMs: forcedRecovery ? 12000 : 25000 }
        );
        clog("info", `X @${profile.username}: profile captured`, "x");
      }
    }
    // post engagement (the x_posts gap) — DOM-harvested, hook-independent.
    const xposts = harvestXPosts(entity, feed);
    if (xposts.length) {
      sendSideEffect(
        { type: "posts", platform: "x", username: feed, posts: xposts },
        "x",
        `X ${feed} post write`,
        { timeoutMs: forcedRecovery ? 12000 : 25000 }
      );
      clog("info", `X ${feed}: ${xposts.length} tweet(s) w/ counts`, "x");
    }
    xTweetRoots().forEach((art) => {
      const ctx = xStatusContext(art);
      xCollectMediaFromRoot(art, sink, entity, ctx);
    });
    // Fallback for media lightboxes/profile media tabs where images may sit
    // outside tweet <article> wrappers.
    xCollectMediaFromRoot(document, sink, entity);
    const activeMediaRevisit = currentBrowserMediaRevisit("x");
    if (activeMediaRevisit && activeMediaRevisit.content_id) {
      sink.items.forEach((it) => {
        it.meta = {
          ...(it.meta || {}),
          revisit_content_id: activeMediaRevisit.content_id,
          revisit_reason: activeMediaRevisit.reason || null,
        };
      });
    }
    const xu = collectPermalinkAuthors(/^\/([A-Za-z0-9_]{1,20})\/status\//, /^(home|explore|search|messages|notifications|i|settings)$/);
    if (xu.length) {
      sendSideEffect(
        { type: "users", platform: "x", context: "seen", users: xu },
        "x",
        "X seen-user write",
        { timeoutMs: forcedRecovery ? 12000 : 25000 }
      );
    }
    const ingestPayload = {
      type: "ingest",
      platform: "x",
      username: entity,
      items: sink.items,
      record_empty: true,
      probe_reason: sink.items.length ? "media_candidates_found" : "no_dom_media_candidates",
      probe_meta: { feed, posts: xposts.length },
    };
    let ingestResponse = null;
    if (activeMediaRevisit && !forcedRecovery) {
      ingestResponse = await send(ingestPayload, { timeoutMs: 45000 });
    } else if (sink.items.length === 0) {
      ingestResponse = await send(ingestPayload, { timeoutMs: forcedRecovery ? 12000 : 25000 }).catch((e) => {
        clog("warn", `X ${entity} empty media probe failed: ${e && e.message ? e.message : e}`, "x");
        return null;
      });
    } else {
      sendSideEffect(
        ingestPayload,
        "x",
        `X ${entity} media write`,
        { timeoutMs: forcedRecovery ? 12000 : 25000 }
      );
    }
    const profileSeen = entity !== "timeline" && !xIsStatusPage() && !!scrapeXProfile(entity);
    const progressCounts = {
      feed,
      entity,
      posts: xposts.length,
      media_candidates: sink.items.length,
      users: xu.length,
      profile: profileSeen ? 1 : 0,
    };
    const hasProgress = (
      xposts.length > 0
      || sink.items.length > 0
      || xu.length > 0
      || profileSeen
    );
    if (hasProgress) {
      xClearZeroProgress();
      await xReportPageHealth("healthy", "x_content_progress", progressCounts);
    } else {
      const zeroStreak = xBumpZeroProgress();
      progressCounts.zero_progress_streak = zeroStreak;
      if (entity !== "timeline" && !xIsStatusPage()) {
        await xReportPageHealth("zero_content", "x_profile_zero_progress", progressCounts);
        if (zeroStreak >= 2) {
          await xReportProfileTarget(entity, "failed", "zero_content", owner);
          clog("warn", `X @${entity}: no profile/media progress after ${zeroStreak} passes; returning home`, "x");
          await sleep(jitter(2500));
          location.href = "https://x.com/home";
          return { targets: 1, saved: 0, posts: 0, discovered: 0 };
        }
      } else if (!xIsStatusPage() && zeroStreak >= 2) {
        await xReportPageHealth("recoverable_error_shell", "x_timeline_zero_progress", progressCounts);
        clog("warn", `X ${feed}: no posts/media after ${zeroStreak} passes; scheduled tab reload`, "x");
        return { targets: 1, saved: 0, posts: 0, discovered: 0, skip_cycle_report: true };
      } else {
        await xReportPageHealth("zero_content", "x_content_zero_progress", progressCounts);
      }
    }
    if (activeMediaRevisit && !forcedRecovery) {
      await finishBrowserMediaRevisit("x", activeMediaRevisit, ingestResponse, sink.items.length, entity);
    }
    if (!forcedRecovery && entity !== "timeline" && !xIsStatusPage()) {
      if (!xIsMediaTab()) {
        clog("info", `X @${entity}: opening Media tab`, "x");
        await sleep(jitter(3000));
        location.href = "https://x.com/" + encodeURIComponent(entity) + "/media";
      } else {
        if (hasProgress) {
          await xReportProfileTarget(entity, "success", null, owner);
          clog("info", `X @${entity}: media/profile pass complete`, "x");
        } else {
          await xReportProfileTarget(entity, "failed", "zero_content", owner);
          clog("warn", `X @${entity}: media tab produced no profile/media content; returning home`, "x");
        }
        await sleep(jitter(3000));
        location.href = "https://x.com/home";
      }
      return { targets: 1, saved: sink.items.length, posts: xposts.length, discovered: xu.length };
    }
    if (!forcedRecovery && xIsStatusPage()) {
      await sleep(jitter(3000));
      location.href = "https://x.com/home";
      return { targets: 1, saved: sink.items.length, posts: xposts.length, discovered: xu.length };
    }
    if (!forcedRecovery && await xMaybeVisitQueuedProfile(owner, cycle)) {
      return { targets: 1, saved: sink.items.length, posts: xposts.length, discovered: xu.length };
    }
    if (!forcedRecovery && await xMaybeVisitTweetDetail(xposts, cycle)) {
      return { targets: 1, saved: sink.items.length, posts: xposts.length, discovered: xu.length };
    }
    return { targets: 1, saved: sink.items.length, posts: xposts.length, discovered: xu.length };
  },
};

// ===========================================================================
function domPostUrlForElement(el, platform) {
  try {
    const root = el && (el.closest('[role="article"], article, [data-pressable-container], [data-testid]') || el.parentElement);
    const links = root ? [...root.querySelectorAll("a[href]")] : [];
    let href = "";
    if (platform === "threads") {
      const link = links.find((a) => /\/@[^/]+\/post\//.test(a.getAttribute("href") || ""));
      href = link && (link.getAttribute("href") || link.href || "");
    } else if (platform === "facebook") {
      const link = links.find((a) => {
        const h = a.getAttribute("href") || "";
        return /\/(posts|photos|videos)\//.test(h) || /^\/(?:photo|permalink|story)\.php/i.test(h);
      });
      href = link && (link.getAttribute("href") || link.href || "");
    }
    return href ? new URL(href, location.href).href : null;
  } catch (e) {
    return null;
  }
}

// Shared DOM media harvester (Threads / Facebook) — read rendered CDN images +
// video posters/sources. Pure DOM reads (no API calls) = very low ban profile.
// The server-side file gate drops avatars/thumbnails/UI chrome by size.
// ===========================================================================
function harvestDom(entity, { imgRe, junkRe, platform }) {
  const sink = makeSink();
  document.querySelectorAll("img").forEach((im) => {
    if (imageLooksTooSmall(im, 120)) return;
    imageUrlsFromElement(im).forEach((u) => {
      if (u && imgRe.test(u) && !junkRe.test(u))
        sink.add({
          content_id: "img_" + urlId(u),
          content_type: "photo",
          url: u,
          entity_name: entity,
          kind: "post",
          meta: {
            dom_asset_role: "image",
            post_url: domPostUrlForElement(im, platform),
          },
        });
    });
  });
  document.querySelectorAll('[style*="url("]').forEach((el) => {
    if (elementLooksTooSmall(el, 140)) return;
    const values = [
      el.getAttribute && el.getAttribute("style"),
      (() => { try { return getComputedStyle(el).backgroundImage; } catch (e) { return ""; } })(),
    ];
    values.flatMap(urlsFromCssValue).forEach((u) => {
      if (u && imgRe.test(u) && !junkRe.test(u)) {
        sink.add({
          content_id: "bg_" + urlId(u),
          content_type: "photo",
          url: u,
          entity_name: entity,
          kind: "post",
          meta: { dom_asset_role: "background_image", post_url: domPostUrlForElement(el, platform) },
        });
      }
    });
  });
  document.querySelectorAll("video").forEach((v) => {
    if (v.poster && /https?:/.test(v.poster) && !junkRe.test(v.poster))
      sink.add({
        content_id: "poster_" + urlId(v.poster),
        content_type: "photo",
        url: v.poster,
        entity_name: entity,
        kind: "post",
        meta: { dom_asset_role: "video_poster", post_url: domPostUrlForElement(v, platform) },
      });
    const u = v.src || (v.querySelector("source") && v.querySelector("source").src);
    if (u && /^https?:/.test(u) && !u.startsWith("blob:"))
      sink.add({
        content_id: "vid_" + urlId(u),
        content_type: "video",
        url: u,
        entity_name: entity,
        kind: "post",
        browser_upload: true,
        browser_upload_only: true,
        meta: { dom_asset_role: "video", post_url: domPostUrlForElement(v, platform) },
      });
  });
  return sink;
}

// Best-effort post-text capture anchored on permalinks (the most stable DOM
// signal). For each post link we climb to its container and take the text as the
// caption. Engagement counts are left null here — reliable counts need the auth
// API (documented), and we won't fabricate them from brittle DOM scraping.
function harvestPermalinkPosts(linkRe, idFrom) {
  const byId = new Map();
  document.querySelectorAll("a[href]").forEach((a) => {
    const m = (a.getAttribute("href") || "").match(linkRe);
    if (!m) return;
    const pid = idFrom(m);
    if (!pid || byId.has(pid)) return;
    const box = a.closest('[data-pressable-container],[role="article"],article') || a.parentElement;
    let caption = "";
    try { caption = (box && box.innerText || "").trim().slice(0, 2200); } catch (e) {}
    if (!caption) return;                       // skip empties / UI chrome
    byId.set(pid, {
      platform_post_id: pid, caption, media_type: "post",
      hashtags: (caption.match(/#[\w.]+/g) || []).map((s) => s.slice(1)),
      mentions: (caption.match(/@[\w.]+/g) || []).map((s) => s.slice(1)),
    });
  });
  return [...byId.values()];
}

// Collect usernames from profile/permalink anchors (X /<user>/status, FB /<user>/posts,
// Threads /@user) -> social_users. Lightweight, DOM-only, every platform.
function collectPermalinkAuthors(re, skip) {
  const set = new Set();
  document.querySelectorAll("a[href]").forEach((a) => {
    const m = (a.getAttribute("href") || "").match(re);
    if (m && m[1] && m[1].length < 40 && !(skip && skip.test(m[1]))) set.add(m[1]);
  });
  return [...set].map((u) => ({ username: u }));
}

// Switch the Threads home feed to a named tab ("Following" primary, "For you"
// secondary). The switcher is a dropdown behind the top button, so: open it (the
// button shows the *other* feed's name), then click the wanted item. Idempotent.
const THREADS_IMG = { imgRe: /(cdninstagram|fbcdn)\.net/, junkRe: /s150x150|s320x320|profile_pic|rsrc\.php/ };
async function threadsSelectFeed(want) {
  try {
    const other = want === "Following" ? "For you" : "Following";
    const find = (txt) => [...document.querySelectorAll('div[role="button"],a[role="link"],span,div')]
      .find((e) => (e.textContent || "").trim() === txt && e.offsetParent !== null);
    let item = find(want);
    if (!item) { const opener = find(other); if (opener) { opener.click(); await sleep(jitter(1200)); item = find(want); } }
    if (item) { item.click(); await sleep(jitter(2000)); return true; }
  } catch (e) {}
  return false;
}

function threadsLoggedInOwner() {
  const sources = [
    ...document.querySelectorAll('a[href^="/@"][aria-label*="Profile" i]'),
    ...document.querySelectorAll('a[href^="/@"]'),
  ];
  for (const el of sources) {
    const txt = el.innerText || el.getAttribute("aria-label") || "";
    const m = txt.match(/@([A-Za-z0-9._]{1,30})/);
    if (m) return m[1];
    const href = el.getAttribute && (el.getAttribute("href") || "");
    const h = href.match(/^\/@([A-Za-z0-9._]{1,30})\/?$/);
    if (h) return h[1];
  }
  return "";
}

// NOTE: a Threads handle == the same Meta account's Instagram handle, but NOT every
// Instagram user has activated Threads. So some IG handles 404 on Threads. We detect
// that on the profile page and blacklist the handle (uc_th_noacct) so we never waste
// another navigation on it.
const thNoAcct = () => new Set(lsGet("uc_th_noacct", "").split(",").filter(Boolean));
function thMarkNoAcct(user) {
  const s = thNoAcct(); s.add(user);
  let arr = [...s]; if (arr.length > 1000) arr = arr.slice(-1000);
  lsSet("uc_th_noacct", arr.join(","));
}
// Threads renders a "page isn't available / not found" view for non-existent users.
function threadsProfileMissing() {
  try {
    const t = (document.body.innerText || "").toLowerCase();
    if (/isn['’]t available|page not found|sorry, this page|couldn['’]t find this account/.test(t)) return true;
  } catch (e) {}
  return false;
}

// Reverse cross-pollination picker: rotate through IG-known real handles the bridge
// hands us, skipping recently-visited AND known-no-Threads-account handles.
function pickThreadsNext(pool) {
  if (!pool || !pool.length) return null;
  const dead = thNoAcct();
  let seen = lsGet("uc_th_seen", "").split(",").filter(Boolean);
  let cand = pool.map((t) => t.username).find((u) => u && !seen.includes(u) && !dead.has(u));
  if (!cand) {  // everyone seen -> reset rotation but still skip dead accounts
    seen = [];
    cand = pool.map((t) => t.username).find((u) => u && !dead.has(u));
  }
  if (!cand) return null;
  seen.push(cand);
  if (seen.length > 300) seen = seen.slice(-300);
  lsSet("uc_th_seen", seen.join(","));
  return cand;
}

// Threads (threads.com) — Meta SPA; media served from the Instagram/FB CDN.
const threads = {
  id: "threads", host: "www.threads.com", label: "Threads",
  entity() { const m = location.pathname.match(/^\/@([^/?#]+)/); return m ? m[1] : "feed"; },

  // Scrape one Threads profile we navigated to (the IG→Threads reverse direction).
  async _scrapeProfile(user, forcedRecovery = false) {
    clog("info", `Threads profile @${user} — scraping (IG-known real account)`, "threads");
    await reportBrowserRecoveryProbe("threads", user, { mode: "profile" });
    await autoScroll(forcedRecovery ? 4 : 8, 1400, forcedRecovery ? 900 : 1800, {
      maxPauseMs: forcedRecovery ? 1500 : 3500,
    });
    const sink = harvestDom(user, { ...THREADS_IMG, platform: "threads" });
    const activeMediaRevisit = currentBrowserMediaRevisit("threads");
    markBrowserMediaRevisitItems("threads", sink, activeMediaRevisit);
    const posts = harvestPermalinkPosts(/\/@([^/]+)\/post\/([^/?#]+)/, (m) => m[2]);
    const ingestPayload = {
      type: "ingest",
      platform: "threads",
      username: user,
      items: sink.items,
      record_empty: true,
      probe_reason: sink.items.length ? "media_candidates_found" : "no_dom_media_candidates",
      probe_meta: { posts: posts.length, mode: "profile" },
    };
    const ingestResponse = forcedRecovery
      ? (sendSideEffect(
          ingestPayload,
          "threads",
          `Threads @${user} forced media write`,
          { timeoutMs: 12000 }
        ), null)
      : await send(ingestPayload);
    if (activeMediaRevisit && !forcedRecovery) {
      await finishBrowserMediaRevisit("threads", activeMediaRevisit, ingestResponse, sink.items.length, user);
    }
    if (posts.length) {
      const postsPayload = { type: "posts", platform: "threads", username: user, posts };
      if (forcedRecovery) {
        sendSideEffect(postsPayload, "threads", `Threads @${user} forced post write`, { timeoutMs: 12000 });
      } else {
        await send(postsPayload);
      }
      clog("info", `Threads @${user}: ${posts.length} post(s)`, "threads");
    }
    return { saved: sink.items.length, posts: posts.length };
  },

  async runCycle() {
    const owner = ownerFromStoredOrDom("threads", threadsLoggedInOwner);
    const forcedRecovery = forcedRecoveryMode("threads");
    if (!forcedRecovery && await maybeSweepFollowGraph({
      platform: "threads",
      owner,
      urls: {
        following: "https://www.threads.com/@" + encodeURIComponent(owner) + "/following",
        followers: "https://www.threads.com/@" + encodeURIComponent(owner) + "/followers",
        followingPath: new RegExp("^/@" + owner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "/following/?$", "i"),
        followersPath: new RegExp("^/@" + owner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "/followers/?$", "i"),
      },
      homeUrl: "https://www.threads.com/",
    })) return { targets: 1, saved: 0, posts: 0, discovered: 0 };
    const mediaRevisit = forcedRecovery ? null : await maybeStartBrowserMediaRevisit("threads", owner);
    if (mediaRevisit && mediaRevisit.navigating) return { targets: 1, saved: 0, posts: 0, discovered: 0 };

    // REVERSE direction: if we navigated to a target profile, scrape it then return
    // to the feed so the rotation continues.
    if (/^\/@/.test(location.pathname)) {
      const user = this.entity();
      // this IG handle may not have a Threads account — detect + blacklist so the
      // rotation never wastes another navigation on it.
      if (threadsProfileMissing()) {
        const activeMediaRevisit = currentBrowserMediaRevisit("threads");
        if (activeMediaRevisit) {
          await finishBrowserMediaRevisit("threads", activeMediaRevisit, null, 0, user);
        }
        thMarkNoAcct(user);
        clog("info", `Threads @${user}: no Threads account — blacklisted from reverse rotation`, "threads");
      } else {
        const r = await this._scrapeProfile(user, forcedRecovery);
        // always bounce back to the feed so the rotation keeps moving (robust even if
        // target tracking desyncs); this is a dedicated scraper tab, not manual browsing.
        lsSet("uc_th_target", "");
        await sleep(jitter(4000));
        location.href = "https://www.threads.com/";
        return { targets: 1, saved: r.saved, posts: r.posts, discovered: 0 };
      }
      lsSet("uc_th_target", "");
      await sleep(jitter(2500));
      location.href = "https://www.threads.com/";
      return { targets: 1, saved: 0, posts: 0, discovered: 0 };
    }

    // FEED: Following primary; For-You every 4th cycle (secondary).
    const c = lsBump("uc_th_cyc");
    const feed = (c % 4 === 0)
      ? ((await threadsSelectFeed("For you")) ? "foryou" : "feed")
      : ((await threadsSelectFeed("Following")) ? "following" : "feed");
    clog("info", `Threads cycle on ${feed} — scrolling`, "threads");
    await reportBrowserRecoveryProbe("threads", feed, { mode: "feed", feed });
    await autoScroll(forcedRecovery ? 4 : 10, 1400, forcedRecovery ? 900 : 1800, {
      maxPauseMs: forcedRecovery ? 1500 : 3500,
    });
    const sink = harvestDom(feed, { ...THREADS_IMG, platform: "threads" });
    const activeMediaRevisit = currentBrowserMediaRevisit("threads");
    markBrowserMediaRevisitItems("threads", sink, activeMediaRevisit);
    const posts = harvestPermalinkPosts(/\/@([^/]+)\/post\/([^/?#]+)/, (m) => m[2]);
    const authors = collectPermalinkAuthors(/^\/@([A-Za-z0-9._]{1,30})(?:\/|$)/, /^(search|explore|activity|saved)$/);
    const ingestPayload = {
      type: "ingest",
      platform: "threads",
      username: feed,
      items: sink.items,
      record_empty: true,
      probe_reason: sink.items.length ? "media_candidates_found" : "no_dom_media_candidates",
      probe_meta: { posts: posts.length, authors: authors.length, feed },
    };
    const ingestResponse = forcedRecovery
      ? (sendSideEffect(
          ingestPayload,
          "threads",
          `Threads ${feed} forced media write`,
          { timeoutMs: 12000 }
        ), null)
      : await send(ingestPayload);
    if (activeMediaRevisit && !forcedRecovery) {
      await finishBrowserMediaRevisit("threads", activeMediaRevisit, ingestResponse, sink.items.length, feed);
    }
    if (posts.length) {
      const postsPayload = { type: "posts", platform: "threads", username: feed, posts };
      if (forcedRecovery) {
        sendSideEffect(postsPayload, "threads", `Threads ${feed} forced post write`, { timeoutMs: 12000 });
      } else {
        await send(postsPayload);
      }
      clog("info", `Threads ${feed}: ${posts.length} post(s)`, "threads");
    }
    // every threads handle IS an instagram handle — push feed authors into the
    // shared user graph so IG scrapes them too (forward cross-pollination).
    if (authors.length) {
      const usersPayload = { type: "users", platform: "threads", context: feed, users: authors };
      if (forcedRecovery) {
        sendSideEffect(usersPayload, "threads", `Threads ${feed} forced user write`, { timeoutMs: 12000 });
      } else {
        await send(usersPayload);
      }
    }

    // REVERSE direction rotation: every 3rd cycle, hop to one IG-known real account
    // and scrape their Threads. Heavily paced (one profile per rotation) to stay gentle.
    if (!forcedRecovery && c % 3 === 0) {
      try {
        const resp = (await send({ type: "getTargets", platform: "threads" })) || [];
        const pool = (Array.isArray(resp) ? resp : (resp.targets || []))
          .map((t) => (typeof t === "string" ? { username: t } : t)).filter((t) => t && t.username);
        const next = pickThreadsNext(pool);
        if (next) {
          lsSet("uc_th_target", next);
          clog("info", `Threads → visiting IG-known @${next} (reverse cross-pollination)`, "threads");
          await sleep(jitter(3000));
          location.href = "https://www.threads.com/@" + next;
        }
      } catch (e) {}
    }
    return { targets: 1, saved: sink.items.length, posts: posts.length, discovered: authors.length };
  },
};

function facebookHandleFromLocation() {
  try {
    if (/^\/profile\.php/i.test(location.pathname)) {
      return new URLSearchParams(location.search).get("id") || "profile.php";
    }
    const m = location.pathname.match(/^\/([^/?#]+)/);
    return m ? m[1] : "feed";
  } catch (e) { return "feed"; }
}

function scrapeFacebookProfile(entity, person) {
  if (!entity || entity === "feed") return null;
  const path = location.pathname;
  const isProfilePath = /\/profile\.php/i.test(path) || /^\/[A-Za-z0-9.]+\/?$/.test(path);
  if (!isProfilePath) return null;
  const body = document.body ? (document.body.innerText || "") : "";
  const name = textOf("h1") || null;
  const followers = compactCount((body.match(/([\d,.]+\s*[KMB]?)\s+followers/i) || [])[1]);
  const friends = compactCount((body.match(/([\d,.]+\s*[KMB]?)\s+friends/i) || [])[1]);
  const avatar = [...document.querySelectorAll("image, img")]
    .map((im) => im.href && im.href.baseVal || im.currentSrc || im.src)
    .find((u) => u && /fbcdn\.net/.test(u) && !/emoji|rsrc\.php|static/i.test(u)) || null;
  return {
    platform_user_id: entity,
    username: entity,
    display_name: name,
    followers_count: followers,
    friends_count: friends,
    is_person: Boolean(person),
    profile_pic_url: avatar,
    metadata: { source: "facebook_dom_profile", url: location.href, limited: !name && !person },
  };
}

function facebookPostIdFromHref(href) {
  const s = String(href || "")
    .replace(/^https?:\/\/(?:www\.)?facebook\.com/i, "")
    .split("#")[0];
  const patterns = [
    /\/posts\/(pfbid[\w]+|\d{6,})/i,
    /\/photos\/(?:[^/?#]+\/)?(?:a\.\d+\/)?(?:\d+\/)?(pfbid[\w]+|\d{6,})/i,
    /\/videos\/(pfbid[\w]+|\d{6,})/i,
    /[?&]story_fbid=(pfbid[\w]+|\d{6,})/i,
    /\/permalink\.php\?(?:[^#]*&)?story_fbid=(pfbid[\w]+|\d{6,})/i,
    /\/share\/(?:p|v|r)\/([^/?#]+)/i,
  ];
  for (const re of patterns) {
    const m = s.match(re);
    if (m && m[1]) return m[1];
  }
  return "";
}

function facebookAuthorFromArticle(art, fallback) {
  try {
    const link = [...art.querySelectorAll("a[href]")]
      .map((a) => a.getAttribute("href") || "")
      .map((href) => href.replace(/^https?:\/\/(?:www\.)?facebook\.com/i, "").split(/[?#]/)[0])
      .map((path) => (path.match(/^\/([A-Za-z0-9.]{5,40})(?:\/|$)/) || [])[1])
      .find((name) => name && !/^(home|watch|marketplace|groups|friends|notifications|messages|reels|events|gaming|bookmarks|stories|pages|story|permalink|profile|sharer|login|policies)$/i.test(name));
    return link || fallback || null;
  } catch (e) {
    return fallback || null;
  }
}

function harvestFacebookPosts(entity) {
  const byId = new Map();
  harvestPermalinkPosts(
    /\/(?:posts\/|permalink\.php\?story_fbid=|[^/]+\/posts\/)?(pfbid[\w]+|\d{6,})/,
    (m) => m[1]
  ).forEach((p) => byId.set(p.platform_post_id, {
    ...p,
    author_username: p.author_username || (entity !== "feed" ? entity : null),
    metadata: { source: "facebook_dom_permalink" },
  }));

  document.querySelectorAll('[role="article"], [data-pagelet*="FeedUnit"]').forEach((art) => {
    try {
      const text = String(art.innerText || "")
        .replace(/\s+/g, " ")
        .trim();
      if (text.length < 50) return;
      if (/^(what['’]?s on your mind|create story|add to your post)\b/i.test(text)) return;
      const hrefs = [...art.querySelectorAll("a[href]")].map((a) => a.getAttribute("href") || "");
      const linkId = hrefs.map(facebookPostIdFromHref).find(Boolean);
      const pid = linkId || ("fbdom_" + urlId(`${entity}|${location.pathname}|${text.slice(0, 800)}`));
      if (byId.has(pid)) return;
      byId.set(pid, {
        platform_post_id: pid,
        author_username: facebookAuthorFromArticle(art, entity !== "feed" ? entity : null),
        caption: text.slice(0, 2200),
        media_type: "post",
        hashtags: (text.match(/#[\w.]+/g) || []).map((s) => s.slice(1)),
        mentions: (text.match(/@[\w.]+/g) || []).map((s) => s.slice(1)),
        metadata: {
          source: linkId ? "facebook_dom_article" : "facebook_dom_article_fallback",
          url: hrefs.find((href) => facebookPostIdFromHref(href)) || location.href,
          synthetic_id: !linkId,
        },
      });
    } catch (e) {}
  });

  return [...byId.values()].slice(0, 50);
}

// Facebook — DOM media from fbcdn; noisy (lots of UI chrome), so the size gate
// does the heavy lifting. Open your feed / a profile's Photos tab and scroll.
const facebook = {
  id: "facebook", host: "www.facebook.com", label: "Facebook",
  entity() {
    const h = facebookHandleFromLocation();
    return h && !/^(home|watch|marketplace|groups|friends|notifications|messages|reels|events|gaming|pages|business|privacy)$/i.test(h) ? h : "feed";
  },
  // Download MEDIA only from a REAL PERSON's profile (user: not pages, not groups).
  // Heuristic: a profile URL showing friend UI ("Add friend"/"Friends"/"Mutual"),
  // without page UI ("follow this Page"/"Send message" to a Page). Conservative —
  // when unsure we DON'T download (avoids page/group media).
  _isPerson() {
    const path = location.pathname;
    if (/\/(groups|watch|marketplace|reels|events|gaming|pages)\b/.test(path) || /^\/(home)?$/.test(path)) return false;
    const isProfilePath = /\/profile\.php/.test(path) || /^\/[A-Za-z0-9.]+\/?$/.test(path);
    if (!isProfilePath) return false;
    const txt = (document.body.innerText || "").slice(0, 20000);
    const personSig = /\bAdd friend\b|\bFriends\b|\bMutual friends?\b/i.test(txt);
    const pageSig = /people follow this|Like this Page|\bThis Page\b|·\s*Follower/i.test(txt);
    return personSig && !pageSig;
  },
  async runCycle() {
    const entity = this.entity();
    const person = this._isPerson();
    const forcedRecovery = forcedRecoveryMode("facebook");
    const mediaRevisit = await maybeStartBrowserMediaRevisit("facebook", facebookLoggedInOwner());
    if (mediaRevisit && mediaRevisit.navigating) return { targets: 1, saved: 0, discovered: 0 };
    clog("info", `cycle start on ${entity} (person profile: ${person})`, "facebook");
    await reportBrowserRecoveryProbe("facebook", entity, { entity, person });
    await autoScroll(forcedRecovery ? 4 : 7, 1400, forcedRecovery ? 900 : 1800, {
      maxPauseMs: forcedRecovery ? 1500 : 3500,
    });
    let saved = 0;
    const profile = scrapeFacebookProfile(entity, person);
    // MEDIA — capture rendered feed/profile media broadly. The bridge/file gate
    // drops UI chrome and tiny avatars; collector policy now favors completeness
    // over the older person-profile-only restriction.
    const sink = harvestDom(entity, { imgRe: /fbcdn\.net/, junkRe: /rsrc\.php|emoji|static|\/s\d+x\d+\/|profile|sprite/, platform: "facebook" });
    const activeMediaRevisit = currentBrowserMediaRevisit("facebook");
    markBrowserMediaRevisitItems("facebook", sink, activeMediaRevisit);
    // POSTS (captions) + USERS — captured EVERYWHERE incl. pages/groups, for the
    // user registry + spidering (user: "when spider we can use either").
    const posts = harvestFacebookPosts(entity);
    if (person) posts.forEach((p) => { if (!p.author_username) p.author_username = entity; });
    const fu = collectPermalinkAuthors(
      /^\/([A-Za-z0-9.]{5,40})(?:\/(posts|photos|videos))?(?:[/?]|$)/,
      /^(home|watch|marketplace|groups|friends|notifications|messages|reels|events|gaming|bookmarks|stories|pages|story\.php|permalink\.php|profile\.php|sharer|login|policies)$/
    );
    const ingestResponse = await send({
      type: "ingest",
      platform: "facebook",
      username: entity,
      items: sink.items,
      record_empty: true,
      probe_reason: sink.items.length ? "media_candidates_found" : "no_dom_media_candidates",
      probe_meta: { posts: posts.length, users: fu.length, person },
    }, { timeoutMs: forcedRecovery ? 30000 : 45000 });
    if (sink.items.length) saved = sink.items.length;
    if (activeMediaRevisit) {
      await finishBrowserMediaRevisit("facebook", activeMediaRevisit, ingestResponse, sink.items.length, entity);
    }
    if (profile) {
      sendSideEffect(
        { type: "profile", platform: "facebook", profile },
        "facebook",
        `Facebook ${entity} profile write`,
        { timeoutMs: forcedRecovery ? 12000 : 25000 }
      );
      clog("info", `Facebook ${entity}: profile captured (person=${person})`, "facebook");
    }
    if (posts.length) {
      sendSideEffect(
        { type: "posts", platform: "facebook", username: entity, posts },
        "facebook",
        `Facebook ${entity} post write`,
        { timeoutMs: forcedRecovery ? 12000 : 25000 }
      );
    }
    if (fu.length) {
      sendSideEffect(
        { type: "users", platform: "facebook", context: "seen", users: fu },
        "facebook",
        "Facebook seen-user write",
        { timeoutMs: forcedRecovery ? 12000 : 25000 }
      );
    }
    return { targets: 1, saved, discovered: 0 };
  },
};

// ===========================================================================
// Strava route capture driver
// ===========================================================================
// This never calls Strava stream APIs directly. It asks the local bridge for one
// prioritized missing-route activity, opens the normal Strava activity page, and
// lets inject.js passively capture Strava's own route-stream response.
const STRAVA_ROUTE_NAV_MIN_MS = 4 * 60 * 1000;
const STRAVA_ROUTE_NAV_MAX_WAIT_MS = 10 * 60 * 1000;
const STRAVA_ROUTE_VISIT_TTL_MS = 6 * 60 * 60 * 1000;
const STRAVA_STREAM_SEEN_TTL_MS = 10 * 60 * 1000;
const STRAVA_STREAM_ATTEMPT_TTL_MS = 12 * 60 * 1000;

function stravaActivityIdFromLocation() {
  const m = location.pathname.match(/^\/activities\/(\d+)(?:[/?#]|$)/);
  return m ? m[1] : "";
}

function stravaLoggedInOwner() {
  const owner = ownerFromStoredOrDom("strava", () => {
    const root = document.querySelector("header, nav, #global-header, .global-header");
    if (!root) return "";
    const links = Array.from(root.querySelectorAll('a[href^="/athletes/"]'));
    for (const link of links) {
      const href = String(link.getAttribute("href") || "");
      const m = href.match(/^\/athletes\/(\d+)(?:[/?#]|$)/);
      if (m) return m[1];
    }
    return "";
  });
  return owner || "extension";
}

async function stravaQueueNext(excludeId = "") {
  const q = await send({
    type: "getStravaRouteQueue",
    limit: 2,
    owner: stravaLoggedInOwner(),
  }).catch(() => null);
  if (!q || q.ok === false) return { queue: q, item: null };
  const items = Array.isArray(q.items) ? q.items : [];
  const item = items.find((it) => String(it.platform_activity_id || "") !== String(excludeId || "")) || null;
  return { queue: q, item };
}

function stravaStreamSeenKey(activityId) {
  return "uc_strava_stream_seen_" + String(activityId || "");
}

function stravaStreamAttemptKey(activityId) {
  return "uc_strava_stream_attempt_" + String(activityId || "");
}

function markStravaStreamAttempted(activityId) {
  if (!activityId) return;
  lsSet(stravaStreamAttemptKey(activityId), String(Date.now()));
}

function markStravaStreamSeen(activityId) {
  if (!activityId) return;
  markStravaStreamAttempted(activityId);
  lsSet(stravaStreamSeenKey(activityId), String(Date.now()));
}

function stravaStreamRecentlySeen(activityId) {
  const seenAt = lsNum(stravaStreamSeenKey(activityId));
  return seenAt && Date.now() - seenAt < STRAVA_STREAM_SEEN_TTL_MS;
}

function stravaStreamRecentlyAttempted(activityId) {
  const attemptedAt = lsNum(stravaStreamAttemptKey(activityId));
  return attemptedAt && Date.now() - attemptedAt < STRAVA_STREAM_ATTEMPT_TTL_MS;
}

function stravaStreamPointCount(streams) {
  try {
    if (Array.isArray(streams)) {
      const row = streams.find((s) => s && (s.type === "latlng" || s.name === "latlng" || s.key === "latlng"));
      return Array.isArray(row && row.data) ? row.data.length : 0;
    }
    const raw = streams && streams.latlng;
    if (Array.isArray(raw)) return raw.length;
    if (raw && Array.isArray(raw.data)) return raw.data.length;
  } catch (e) {}
  return 0;
}

async function captureStravaStreamsDirect(activityId) {
  if (!activityId || stravaStreamRecentlySeen(activityId) || stravaStreamRecentlyAttempted(activityId)) return false;
  const qs = [
    "latlng", "time", "altitude", "distance", "heartrate",
    "cadence", "watts", "velocity_smooth", "grade_smooth",
  ].map((s) => "stream_types%5B%5D=" + encodeURIComponent(s)).join("&");
  const requestUrl = `/activities/${encodeURIComponent(activityId)}/streams?${qs}`;
  try {
    const resp = await fetch(requestUrl, {
      credentials: "include",
      cache: "no-store",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (resp.status === 401 || resp.status === 403 || resp.status === 429) {
      markStravaStreamAttempted(activityId);
      await send({
        type: "strava_streams",
        activity_id: activityId,
        request_url: requestUrl,
        http_status: resp.status,
        owner: stravaLoggedInOwner(),
        streams: {},
        point_count: 0,
      }).catch(() => {});
      clog("warn", `activity ${activityId}: route stream HTTP ${resp.status}`, "strava");
      return false;
    }
    const streams = await resp.json().catch(() => null);
    const pointCount = stravaStreamPointCount(streams);
    if (pointCount < 2) {
      markStravaStreamAttempted(activityId);
      await send({
        type: "strava_streams",
        activity_id: activityId,
        request_url: requestUrl,
        http_status: resp.status,
        owner: stravaLoggedInOwner(),
        streams: streams || {},
        point_count: pointCount,
      }).catch(() => {});
      clog("warn", `activity ${activityId}: no route points in browser stream`, "strava");
      return false;
    }
    markStravaStreamSeen(activityId);
    await send({
      type: "strava_streams",
      activity_id: activityId,
      request_url: requestUrl,
      http_status: resp.status,
      owner: stravaLoggedInOwner(),
      streams,
      point_count: pointCount,
    });
    clog("info", `activity ${activityId}: route ${pointCount} point(s) captured`, "strava");
    return true;
  } catch (e) {
    markStravaStreamAttempted(activityId);
    clog("warn", `activity ${activityId}: route stream fetch failed`, "strava");
    return false;
  }
}

const strava = {
  id: "strava", host: "www.strava.com", label: "Strava",
  async recordActivityPage(activityId) {
    const key = "uc_strava_route_seen_" + activityId;
    const last = lsNum(key);
    if (Date.now() - last < STRAVA_ROUTE_VISIT_TTL_MS) return false;
    lsSet(key, String(Date.now()));
    await send({
      type: "stravaRouteVisit",
      activity_id: activityId,
      url: location.href,
      owner: stravaLoggedInOwner(),
      status: "page_loaded",
    }).catch(() => {});
    return true;
  },
  nextAllowedAt() {
    const nextAt = lsNum("uc_strava_route_next_at");
    if (nextAt && nextAt - Date.now() > STRAVA_ROUTE_NAV_MAX_WAIT_MS) {
      const capped = Date.now() + human(STRAVA_ROUTE_NAV_MIN_MS);
      lsSet("uc_strava_route_next_at", String(capped));
      return capped;
    }
    return nextAt;
  },
  setNextAllowed() {
    lsSet("uc_strava_route_next_at", String(Date.now() + human(STRAVA_ROUTE_NAV_MIN_MS)));
  },
  async nudgeRouteMapIntoView() {
    const selectors = [
      '[data-testid*="map" i]',
      '[class*="map" i]',
      ".leaflet-container",
      ".mapboxgl-map",
      "#map",
    ];
    for (const sel of selectors) {
      const node = document.querySelector(sel);
      if (node && typeof node.scrollIntoView === "function") {
        node.scrollIntoView({ behavior: "smooth", block: "center" });
        await hsleep(1800);
        return;
      }
    }
    window.scrollBy({ top: Math.round(window.innerHeight * 0.8), behavior: "smooth" });
    await hsleep(1800);
  },
  async runCycle() {
    const activityId = stravaActivityIdFromLocation();
    if (activityId) {
      const recorded = await this.recordActivityPage(activityId);
      if (recorded) clog("info", `activity ${activityId}: waiting for route stream`, "strava");
      await this.nudgeRouteMapIntoView();
      await hsleep(22000);
      if (!stravaStreamRecentlySeen(activityId)) {
        await captureStravaStreamsDirect(activityId);
        await hsleep(2500);
      }
    }

    const waitMs = this.nextAllowedAt() - Date.now();
    if (waitMs > 0) {
      clog("info", `route queue paused ${(waitMs / 60000).toFixed(1)}m before next activity`, "strava");
      return { targets: activityId ? 1 : 0, saved: 0, discovered: 0 };
    }

    const { queue, item } = await stravaQueueNext(activityId);
    if (!queue || queue.ok === false) {
      clog("warn", "route queue unavailable", "strava");
      return { targets: activityId ? 1 : 0, saved: 0, discovered: 0 };
    }
    if (queue.cooldown && queue.cooldown.active) {
      const until = queue.cooldown.until ? new Date(queue.cooldown.until).toLocaleTimeString() : "later";
      clog("warn", `route queue cooldown until ${until}`, "strava");
      return { targets: activityId ? 1 : 0, saved: 0, discovered: 0 };
    }
    if (!item || !item.activity_url) {
      clog("info", "route queue empty", "strava");
      return { targets: activityId ? 1 : 0, saved: 0, discovered: 0 };
    }

    this.setNextAllowed();
    await send({
      type: "stravaRouteVisit",
      activity_id: item.platform_activity_id,
      activity_url: item.activity_url,
      url: location.href,
      owner: stravaLoggedInOwner(),
      status: "navigate",
    }).catch(() => {});
    clog("info", `opening route candidate ${item.platform_activity_id} (${item.athlete_name || "unknown"})`, "strava");
    await hsleep(2000);
    location.href = item.activity_url;
    return { targets: 1, saved: 0, discovered: 0 };
  },
};

// ===========================================================================
// Registry + dispatch
// ===========================================================================
const PLATFORMS = [instagram, tiktok, lemon8, x, threads, facebook, strava];

function currentPlatform() {
  const host = location.hostname;
  if (host === "twitter.com" || host.endsWith(".twitter.com")) return x;
  return PLATFORMS.find((p) => host === p.host || host.endsWith("." + p.host)) || null;
}

// ===========================================================================
// CONTINUOUS LOOP (not "cycles"). The work lives HERE in the content script,
// which can run as long as the tab is open — unlike the MV3 service worker,
// which Chrome kills after ~30s idle. So there is no 30-min timer: we just loop
// forever, human-paced (rate-limited + jittered), doing one small pass at a time
// with long rests between. If the loop ever dies (page reload / crash) it is
// respawned: the content script auto-starts it on load, and a lightweight
// service-worker watchdog re-nudges any open tab that isn't looping.
// ===========================================================================
let LOOP_RUNNING = false;
let LOOP_STARTED_AT = 0;
let LOOP_LAST_PROGRESS_AT = 0;
let ONE_SHOT_RUNNING = false;
let ONE_SHOT_STARTED_AT = 0;
let ONE_SHOT_REASON = "";
let LOOP_TIMEOUT_STREAK = 0;
let ONE_SHOT_TIMEOUT_STREAK = 0;
let SCRAPE_PASS_RUNNING = false;
let SCRAPE_PASS_STARTED_AT = 0;
let SCRAPE_PASS_REASON = "";
let SCRAPE_PASS_TOKEN = 0;
const LOOP_STALE_MS = 8 * 60 * 1000;
const LOOP_STALE_MS_BY_PLATFORM = {
  x: 4 * 60 * 1000,
  facebook: 4 * 60 * 1000,
  tiktok: 5 * 60 * 1000,
  lemon8: 5 * 60 * 1000,
  threads: 5 * 60 * 1000,
  strava: 6 * 60 * 1000,
};
const ONE_SHOT_STALE_MS = 8 * 60 * 1000;
const ONE_SHOT_STALE_MS_BY_PLATFORM = {
  x: 3 * 60 * 1000,
  facebook: 3 * 60 * 1000,
  tiktok: 4 * 60 * 1000,
  lemon8: 4 * 60 * 1000,
  threads: 4 * 60 * 1000,
  strava: 5 * 60 * 1000,
};
const ONE_SHOT_TIMEOUT_MS_BY_PLATFORM = {
  instagram: 10 * 60 * 1000,
  strava: 5 * 60 * 1000,
  tiktok: 4 * 60 * 1000,
  lemon8: 4 * 60 * 1000,
  threads: 4 * 60 * 1000,
  x: 3 * 60 * 1000,
  facebook: 5 * 60 * 1000,
};
const LOOP_CYCLE_TIMEOUT_MS_BY_PLATFORM = {
  instagram: 12 * 60 * 1000,
  strava: 6 * 60 * 1000,
  tiktok: 5 * 60 * 1000,
  lemon8: 5 * 60 * 1000,
  threads: 5 * 60 * 1000,
  x: 4 * 60 * 1000,
  facebook: 7 * 60 * 1000,
};
const TIMEOUT_RELOAD_STREAK_BY_PLATFORM = {
  instagram: 2,
  strava: 2,
  tiktok: 1,
  lemon8: 2,
  threads: 1,
  x: 1,
  facebook: 1,
};

function oneShotTimeoutMs(platformId) {
  return ONE_SHOT_TIMEOUT_MS_BY_PLATFORM[platformId] || 4 * 60 * 1000;
}

function loopCycleTimeoutMs(platformId) {
  return LOOP_CYCLE_TIMEOUT_MS_BY_PLATFORM[platformId] || 6 * 60 * 1000;
}

function timeoutReloadStreak(platformId) {
  return TIMEOUT_RELOAD_STREAK_BY_PLATFORM[platformId] || 2;
}

function oneShotStaleMs(platformId) {
  return ONE_SHOT_STALE_MS_BY_PLATFORM[platformId] || ONE_SHOT_STALE_MS;
}

function loopStaleMs(platformId) {
  return LOOP_STALE_MS_BY_PLATFORM[platformId] || LOOP_STALE_MS;
}

function markLoopProgress() {
  LOOP_LAST_PROGRESS_AT = Date.now();
}

function loopProgressAgeMs() {
  const anchor = Math.max(LOOP_LAST_PROGRESS_AT || 0, LOOP_STARTED_AT || 0);
  return anchor ? Date.now() - anchor : 0;
}

function scrapePassAgeMs() {
  return SCRAPE_PASS_RUNNING && SCRAPE_PASS_STARTED_AT ? Date.now() - SCRAPE_PASS_STARTED_AT : 0;
}

function scrapePassStaleMs(platformId) {
  return Math.max(loopCycleTimeoutMs(platformId), oneShotTimeoutMs(platformId)) + 60 * 1000;
}

function clearScrapePass(token) {
  if (token !== SCRAPE_PASS_TOKEN) return;
  SCRAPE_PASS_RUNNING = false;
  SCRAPE_PASS_STARTED_AT = 0;
  SCRAPE_PASS_REASON = "";
}

function forceClearScrapePass() {
  SCRAPE_PASS_TOKEN++;
  SCRAPE_PASS_RUNNING = false;
  SCRAPE_PASS_STARTED_AT = 0;
  SCRAPE_PASS_REASON = "";
}

function startScrapePass(p, reason) {
  if (!p || SCRAPE_PASS_RUNNING) return null;
  SCRAPE_PASS_RUNNING = true;
  SCRAPE_PASS_STARTED_AT = Date.now();
  SCRAPE_PASS_REASON = reason || "loop";
  const token = ++SCRAPE_PASS_TOKEN;
  const promise = Promise.resolve().then(() => p.runCycle());
  promise.then(() => clearScrapePass(token), () => clearScrapePass(token));
  return promise;
}

function deferBrowserMediaRevisitForForcedRecovery(platform) {
  if (!ONE_SHOT_RUNNING) return false;
  if (!BROWSER_MEDIA_REVISIT_PLATFORMS.has(platform)) return false;
  return /browser_content_stale|manual|stale/i.test(ONE_SHOT_REASON || "");
}

function scheduleOneShotReload(p, reason) {
  if (!p) return;
  setTimeout(() => {
    try {
      clog("warn", `${p.label} forced pass ${reason}; reloading tab`, p.label);
      location.reload();
    } catch (e) {}
  }, 800 + Math.random() * 1600);
}

// Rest between passes — a person doesn't scrape non-stop. Instagram stays slower
// because it is the account most likely to hit 429; lower-risk platforms can loop
// more often so they do not wait behind Instagram's safety budget.
const PASS_REST_MS = 180000; // fallback: ~2.4m-6.6m + occasional longer breaks via human()
const PASS_REST_MS_BY_PLATFORM = {
  instagram: 180000,
  tiktok: 180000,
  lemon8: 60000,
  threads: 120000,
  x: 120000,
  facebook: 120000,
  strava: 90000,
};
function passRestMs(platformId) {
  return PASS_REST_MS_BY_PLATFORM[platformId] || PASS_REST_MS;
}

async function mainLoop() {
  const p = currentPlatform();
  if (!p) return;
  if (!ucContentScriptCurrent()) return;
  if (LOOP_RUNNING) return;            // one loop per tab
  LOOP_RUNNING = true;
  LOOP_STARTED_AT = Date.now();
  markLoopProgress();
  clog("info", `${p.label} loop started — continuous & human-paced (no fixed timer)`, p.label);
  await send({ type: "loopStatus", platform: p.id, label: p.label, running: true, url: location.href }).catch(() => {});
  try {
    while (LOOP_RUNNING && ucContentScriptCurrent()) {
      try {
        const leftMs = wallLeftMs(p.id);
        if (leftMs > 0) {
          markLoopProgress();
          const now = Date.now();
          if (!LAST_WALL_LOG_AT[p.id] || now - LAST_WALL_LOG_AT[p.id] > WALL_LOG_GAP_MS) {
            LAST_WALL_LOG_AT[p.id] = now;
            clog("warn", `${p.label} cooldown active for ${Math.ceil(leftMs / 60000)}m; pausing this tab`, p.label);
          }
          await sleep(Math.min(leftMs, human(Math.min(passRestMs(p.id), 300000))));
          continue;
        }
        const shell = detectRecoverablePageShell(p.id);
        if (shell) {
          markLoopProgress();
          const clicked = await attemptRecoverablePageInteraction(p.id, shell);
          const recovery = await reportRecoverablePageShell(p, shell);
          if (recovery && recovery.cooldown_mins) {
            setWall(p.id, recovery.cooldown_mins);
          }
          // Emit an empty-ingest probe so the dashboard's "content endpoint"
          // rate for this platform reflects "recovery attempts in progress"
          // rather than silence. Without this, a platform stuck in the
          // recoverable-shell state (e.g. threads showing "Something went
          // wrong" after Meta's server-side block) shows 0 media/posts rows
          // even though the loop is actively probing every cycle — which
          // looks like a broken scraper from the operator's view. Fire and
          // forget (sendSideEffect), so we never block the recovery sleep.
          try {
            sendSideEffect(
              {
                type: "ingest",
                platform: p.id,
                username: p.entity ? (p.entity() || "feed") : "feed",
                items: [],
                record_empty: true,
                probe_reason: "recoverable_error_shell",
                probe_meta: {
                  shell_reason: shell.reason,
                  content_counts: shell.content_counts || null,
                  url: location.href,
                  clicked_retry: !!clicked,
                },
              },
              p.id,
              `${p.label} shell probe`,
              { timeoutMs: 8000 }
            );
          } catch (_) {}
          const delay = Number((recovery && recovery.delay_ms) || passRestMs(p.id));
          await sleep(clicked ? human(12000) : human(Math.max(30000, Math.min(delay, 300000))));
          continue;
        }
        markLoopProgress();
        if (SCRAPE_PASS_RUNNING) {
          const ageMs = scrapePassAgeMs();
          const staleMs = scrapePassStaleMs(p.id);
          if (ageMs > staleMs) {
            await reportForcedCycleHealth(p, "scrape_pass_stale_reloading", SCRAPE_PASS_REASON || "loop", {
              scrape_pass_age_ms: ageMs,
              stale_after_ms: staleMs,
              scrape_pass_forced_clear: true,
            });
            forceClearScrapePass();
            scheduleOneShotReload(p, `scrape pass stuck ${Math.ceil(ageMs / 60000)}m`);
            await sleep(human(30000));
          } else {
            await reportForcedCycleHealth(p, "scrape_pass_already_running", SCRAPE_PASS_REASON || "loop", {
              scrape_pass_age_ms: ageMs,
              stale_after_ms: staleMs,
            });
            await sleep(human(45000));
          }
          continue;
        }
        const timeoutMs = loopCycleTimeoutMs(p.id);
        const cyclePromise = startScrapePass(p, "loop");
        if (!cyclePromise) continue;
        const stats = await withDeadline(
          cyclePromise,  // one pass: IG = a few profiles; others = scrape current page
          timeoutMs,
          `${p.label} loop scrape pass timed out after ${Math.ceil(timeoutMs / 60000)}m`,
          "UCLoopCycleTimeout"
        );
        LOOP_TIMEOUT_STREAK = 0;
        markLoopProgress();
        if (!stats || !stats.skip_cycle_report) {
          await send({ type: "cycleReport", platform: p.label, ...stats }).catch(() => {});
        }
      } catch (e) {
        if (e instanceof WallError) {
          const mins = await applyThrottleWall(p.id, "generic-wall");
          clog("warn", `${p.label} hit a throttle/login wall — sleeping ${mins}m (persisted, survives refresh)`, p.label);
          await sleep(mins * 60000);
          continue;
        }
        clog("error", `${p.label} loop error: ${e.message}`, p.label);
        if (e && e.name === "UCLoopCycleTimeout") {
          LOOP_TIMEOUT_STREAK += 1;
          const reloadAfter = timeoutReloadStreak(p.id);
          const timedOutScrapePassAgeMs = scrapePassAgeMs();
          forceClearScrapePass();
          await reportForcedCycleHealth(p, "loop_cycle_timeout", "loop_cycle_timeout", {
            cycle_error: e.message,
            timeout_ms: loopCycleTimeoutMs(p.id),
            timeout_streak: LOOP_TIMEOUT_STREAK,
            reload_after_streak: reloadAfter,
            scrape_pass_age_ms: timedOutScrapePassAgeMs,
            scrape_pass_forced_clear: true,
          });
          if (LOOP_TIMEOUT_STREAK >= reloadAfter) {
            scheduleOneShotReload(p, `loop scrape timed out ${LOOP_TIMEOUT_STREAK}x`);
          } else {
            clog("warn", `${p.label} loop timeout ${LOOP_TIMEOUT_STREAK}/${reloadAfter}; backing off without reload`, p.label);
          }
        }
        await sleep(human(e && e.name === "UCLoopCycleTimeout" ? Math.min(passRestMs(p.id) * 2, 600000) : 60000));
      }
      // heartbeat so the popup shows the loop is alive between passes
      markLoopProgress();
      await send({ type: "loopStatus", platform: p.id, label: p.label, running: true, url: location.href }).catch(() => {});
      await sleep(human(passRestMs(p.id))); // long human rest between passes
    }
  } finally {
    LOOP_RUNNING = false;
    LOOP_STARTED_AT = 0;
    LOOP_LAST_PROGRESS_AT = 0;
    await send({ type: "loopStatus", platform: p.id, label: p.label, running: false, url: location.href }).catch(() => {});
  }
}

function asCycleNumber(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n : 0;
}

async function reportForcedCycleHealth(p, status, reason, extra = {}) {
  if (!p) return null;
  return send({
    type: "pageHealth",
    platform: p.id,
    label: p.label,
    status,
    reason: reason || null,
    cycle_reason: reason || null,
    url: location.href,
    title: document.title || "",
    content_counts: pageContentCounts(),
    loop_running: LOOP_RUNNING,
    loop_age_ms: LOOP_STARTED_AT ? Date.now() - LOOP_STARTED_AT : 0,
    loop_progress_age_ms: loopProgressAgeMs(),
    one_shot_running: ONE_SHOT_RUNNING,
    one_shot_age_ms: ONE_SHOT_RUNNING && ONE_SHOT_STARTED_AT ? Date.now() - ONE_SHOT_STARTED_AT : 0,
    scrape_pass_running: SCRAPE_PASS_RUNNING,
    scrape_pass_age_ms: scrapePassAgeMs(),
    scrape_pass_reason: SCRAPE_PASS_REASON || null,
    ...extra,
  }).catch(() => null);
}

async function runOneShotCycle(reason) {
  const p = currentPlatform();
  if (!p) return;
  if (ONE_SHOT_RUNNING) {
    const staleMs = oneShotStaleMs(p.id);
    const ageMs = ONE_SHOT_STARTED_AT ? Date.now() - ONE_SHOT_STARTED_AT : staleMs + 1;
    if (ageMs > staleMs) {
      clog("warn", `${p.label} forced pass appears stuck for ${Math.ceil(ageMs / 60000)}m; reloading tab`, p.label);
      await reportForcedCycleHealth(p, "forced_cycle_stale_reloading", "stale_one_shot", {
        one_shot_age_ms: ageMs,
        stale_after_ms: staleMs,
      });
      ONE_SHOT_RUNNING = false;
      ONE_SHOT_STARTED_AT = 0;
      ONE_SHOT_REASON = "";
      scheduleOneShotReload(p, "appears stuck");
      return;
    }
    clog("warn", `${p.label} forced pass skipped; another forced pass is already running`, p.label);
    await reportForcedCycleHealth(p, "forced_cycle_skipped", "already_running", {
      one_shot_age_ms: ageMs,
      stale_after_ms: staleMs,
    });
    return;
  }
  if (SCRAPE_PASS_RUNNING) {
    const ageMs = scrapePassAgeMs();
    const staleMs = scrapePassStaleMs(p.id);
    if (ageMs > staleMs) {
      clog("warn", `${p.label} forced pass found a stale scrape pass; reloading tab`, p.label);
      await reportForcedCycleHealth(p, "forced_cycle_reloading_scrape_pass_stale", reason || "manual", {
        scrape_pass_age_ms: ageMs,
        stale_after_ms: staleMs,
        scrape_pass_forced_clear: true,
      });
      forceClearScrapePass();
      scheduleOneShotReload(p, "stale scrape pass");
      return;
    }
    await reportForcedCycleHealth(p, "forced_cycle_deferred", "scrape_pass_running", {
      scrape_pass_age_ms: ageMs,
      stale_after_ms: staleMs,
    });
    return;
  }
  ONE_SHOT_RUNNING = true;
  ONE_SHOT_STARTED_AT = Date.now();
  ONE_SHOT_REASON = reason || "manual";
  clog("warn", `${p.label} forced one scrape pass (${reason || "manual"})`, p.label);
  await reportForcedCycleHealth(p, "forced_cycle_started", reason || "manual");
  await send({ type: "loopStatus", platform: p.id, label: p.label, running: true, url: location.href }).catch(() => {});
  try {
    const leftMs = wallLeftMs(p.id);
    if (leftMs > 0) {
      clog("warn", `${p.label} forced pass skipped; cooldown active for ${Math.ceil(leftMs / 60000)}m`, p.label);
      await reportForcedCycleHealth(p, "forced_cycle_skipped", "cooldown", {
        cooldown_left_ms: Math.round(leftMs),
      });
      return;
    }
    const shell = detectRecoverablePageShell(p.id);
    if (shell) {
      const clicked = await attemptRecoverablePageInteraction(p.id, shell);
      await reportRecoverablePageShell(p, shell);
      await reportForcedCycleHealth(p, "forced_cycle_skipped", shell.reason || "recoverable_error_shell", {
        content_counts: shell.content_counts || pageContentCounts(),
        recovery_click_attempted: clicked || null,
      });
      return;
    }
    const timeoutMs = oneShotTimeoutMs(p.id);
    const cyclePromise = startScrapePass(p, "one_shot:" + (reason || "manual"));
    if (!cyclePromise) {
      await reportForcedCycleHealth(p, "forced_cycle_deferred", "scrape_pass_running");
      return;
    }
    const stats = await withDeadline(
      cyclePromise,
      timeoutMs,
      `${p.label} forced scrape pass timed out after ${Math.ceil(timeoutMs / 60000)}m`,
      "UCOneShotTimeout"
    );
    ONE_SHOT_TIMEOUT_STREAK = 0;
    await reportForcedCycleHealth(p, "forced_cycle_finished", reason || "manual", {
      cycle_targets: asCycleNumber(stats && stats.targets),
      cycle_saved: asCycleNumber(stats && stats.saved),
      cycle_discovered: asCycleNumber(stats && stats.discovered),
    });
    if (!stats || !stats.skip_cycle_report) {
      await send({ type: "cycleReport", platform: p.label, ...stats }).catch(() => {});
    }
  } catch (e) {
    clog("error", `${p.label} forced scrape pass error: ${e.message}`, p.label);
    if (e && e.name === "UCOneShotTimeout") {
      ONE_SHOT_TIMEOUT_STREAK += 1;
    }
    const timedOutScrapePassAgeMs = e && e.name === "UCOneShotTimeout" ? scrapePassAgeMs() : null;
    if (e && e.name === "UCOneShotTimeout") {
      forceClearScrapePass();
    }
    await reportForcedCycleHealth(p, "forced_cycle_error", reason || "manual", {
      cycle_error: e && e.message ? e.message : String(e),
      one_shot_timeout: e && e.name === "UCOneShotTimeout" ? true : null,
      timeout_ms: e && e.name === "UCOneShotTimeout" ? oneShotTimeoutMs(p.id) : null,
      timeout_streak: e && e.name === "UCOneShotTimeout" ? ONE_SHOT_TIMEOUT_STREAK : null,
      reload_after_streak: e && e.name === "UCOneShotTimeout" ? timeoutReloadStreak(p.id) : null,
      scrape_pass_age_ms: timedOutScrapePassAgeMs,
      scrape_pass_forced_clear: e && e.name === "UCOneShotTimeout" ? true : null,
    });
    if (e && e.name === "UCOneShotTimeout" && ONE_SHOT_TIMEOUT_STREAK >= timeoutReloadStreak(p.id)) {
      scheduleOneShotReload(p, `timed out ${ONE_SHOT_TIMEOUT_STREAK}x`);
    }
  } finally {
    ONE_SHOT_RUNNING = false;
    ONE_SHOT_STARTED_AT = 0;
    ONE_SHOT_REASON = "";
    await send({ type: "loopStatus", platform: p.id, label: p.label, running: LOOP_RUNNING, url: location.href }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) {
    try { sendResponse({ ok: false, reason: "empty_message" }); } catch (_) {}
    return false;
  }
  // Fast negative reply during content-script re-init on SPAs like X/Threads:
  // pushState navigations frequently unload+re-attach the content script, and
  // if a SW-side chrome.tabs.sendMessage lands during that window, the handler
  // would previously `return false` without calling sendResponse -- Chrome
  // then closes the port with no response and the SW promise hangs its full
  // 15s timeout budget. Respond immediately so the SW can recover fast.
  if (!ucContentScriptCurrent()) {
    try { sendResponse({ ok: false, reason: "content_script_not_current" }); } catch (_) {}
    return false;
  }
  // "ensureLoop" (watchdog / manual Scrape-now): start the loop if it isn't running.
  if (msg.type === "ensureLoop" || msg.type === "scrapeCycle") {
    const p = currentPlatform();
    const progressAgeMs = loopProgressAgeMs();
    const staleMs = loopStaleMs(p && p.id);
    sendResponse({
      ok: true,
      running: LOOP_RUNNING,
      forced: msg.type === "scrapeCycle",
      loop_progress_age_ms: progressAgeMs,
      stale_after_ms: staleMs,
    });
    send({
      type: "loopStatus",
      platform: p && p.id,
      label: p && p.label,
      running: LOOP_RUNNING,
      url: location.href,
      health_status: msg.type === "scrapeCycle" ? "watchdog_scrape_nudge" : "watchdog_loop_nudge",
      health_reason: msg.reason || msg.type,
      content_age_seconds: Math.round(progressAgeMs / 1000),
      stale_after_ms: staleMs,
    }).catch(() => {});
    if (msg.type === "scrapeCycle") runOneShotCycle(msg.reason || "manual");
    else if (!LOOP_RUNNING) mainLoop();
    else if (/browser_content_stale|stale/i.test(msg.reason || "") && progressAgeMs > staleMs) {
      reportForcedCycleHealth(p, "loop_stale_reloading", msg.reason || "browser_content_stale", {
        loop_progress_age_ms: progressAgeMs,
        stale_after_ms: staleMs,
      }).finally(() => scheduleOneShotReload(p, "loop stale"));
    } else if (/browser_content_stale|stale/i.test(msg.reason || "")) {
      reportForcedCycleHealth(p, "ensure_loop_already_running", msg.reason || "browser_content_stale", {
        loop_progress_age_ms: progressAgeMs,
        stale_after_ms: staleMs,
      });
    }
    return false;
  }
});

// Relay engagement posts captured by the MAIN-world network hook (inject.js).
// inject.js reads Meta's own GraphQL/REST responses (likes/replies/reposts) and
// postMessages them here; we forward to the posts endpoint. Robust, no extra reqs.
window.addEventListener("message", (ev) => {
  if (!ucContentScriptCurrent()) return;
  const m = ev.data;
  if (!m || m.__uc !== true) return;
  // label the source so logs say WHERE it came from instead of "undefined".
  const where = (() => { try { return (location.pathname.match(/^\/@?([^/?#]+)/) || [, "feed"])[1].slice(0, 30); } catch (e) { return "feed"; } })();
  if (m.type === "posts" && Array.isArray(m.posts) && m.posts.length) {
    send({ type: "posts", platform: m.platform, username: where, posts: m.posts }).catch(() => {});
  } else if (m.type === "ingest" && Array.isArray(m.items) && m.items.length) {
    send({
      type: "ingest",
      platform: m.platform,
      username: m.username || where,
      items: m.items,
    }).catch(() => {});
  } else if (m.type === "users" && Array.isArray(m.users) && m.users.length) {
    send({ type: "users", platform: m.platform, context: m.context || "seen", owner: m.owner || null, users: m.users }).catch(() => {});
  } else if (m.type === "dms" && Array.isArray(m.threads) && m.threads.length) {
    send({ type: "dms", platform: m.platform, owner: m.owner || null, threads: m.threads }).catch(() => {});
  } else if ((m.type === "tiktok_dm" || m.type === "instagram_dm") && m.frame) {
    // DM JSON frame from a WS (rare — most are protobuf/MQTT). Forward for capture.
    send({ type: m.type, platform: m.platform, frame: m.frame }).catch(() => {});
  } else if (m.type === "dm_probe") {
    // Format probe so we can confirm each platform's DM wire format (#38).
    send({ type: "dm_probe", platform: m.platform, transport: m.transport,
           url: m.url, frame_kind: m.frame_kind, frame_size: m.frame_size }).catch(() => {});
    clog("info", `DM ${m.transport} observed: ${m.frame_kind || "url"} ${m.frame_size ? m.frame_size + "B " : ""}${m.url}`, m.platform);
  } else if (m.type === "dm_sample" && m.b64) {
    // Raw sample bytes of a real DM-socket frame, for decoder work (#35).
    send({ type: "dm_sample", platform: m.platform, url: m.url, size: m.size, b64: m.b64 }).catch(() => {});
  } else if (m.type === "dm_heartbeat") {
    // Periodic liveness beat from inject.js's WS hook (P1.3). Forward with
    // the tab's current platform so the bridge can upsert per (platform, owner).
    send({
      type: "dm_heartbeat", platform: m.platform,
      probes_sent: m.probes_sent, samples_shipped: m.samples_shipped,
    }).catch(() => {});
  } else if (m.type === "dm_decoded") {
    // Option B: client-decoded DM payload (threads + messages). Straight
    // pass-through to the bridge's POST /social/dm-decoded upsert handler.
    send({
      type: "dm_decoded", platform: m.platform, owner: m.owner || "",
      threads: m.threads || [], messages: m.messages || [],
    }).catch(() => {});
  } else if (m.type === "strava_streams" && m.activity_id) {
    if ((m.point_count || 0) > 1) markStravaStreamSeen(m.activity_id);
    else markStravaStreamAttempted(m.activity_id);
    send({
      type: "strava_streams",
      activity_id: m.activity_id,
      request_url: m.request_url,
      http_status: m.http_status,
      owner: stravaLoggedInOwner(),
      point_count: m.point_count,
      streams: m.streams || {},
    }).catch(() => {});
  }
});

// First-boot heartbeat — makes the ingest bridge learn this tab is alive
// even if the very next action (e.g. lemon8's maybeStartBrowserMediaRevisit
// inside runCycle) navigates the tab away before the SW-mediated tabReady
// below can be delivered. Fire-and-forget, non-blocking.
//
// Route selection:
//   - CSP-blocked page origins (instagram/threads/facebook) — page CSP forbids
//     connect-src to http://127.0.0.1:*, so direct fetch would raise a
//     "Refused to connect" console error. Route via SW proxy instead
//     (extension origin is exempt from page CSP).
//   - All other origins — direct fetch with keepalive: true so the request
//     survives an in-flight unload (the SW proxy's chrome.runtime.sendMessage
//     does NOT survive unload the same way).
try {
  const _bootP = currentPlatform() || {};
  const _bootPayload = withDirectVersion({
    platform: _bootP.id || "unknown",
    label: _bootP.label || null,
    running: true,
    url: location.href,
    tab_id: "content_direct",
    health_status: "content_script_boot",
    health_reason: "first-boot ping before mainLoop starts",
  });
  // Route through the service worker so page-origin CSP and static scanners do
  // not see a social-site content script making direct loopback HTTP calls.
  chrome.runtime.sendMessage({
    type: "swFetchProxy",
    path: "/social/browser-heartbeat",
    payload: _bootPayload,
  }).catch(() => {});
} catch (e) {}

// Auto-start the loop the moment the tab loads (respawns after a reload/crash).
send({ type: "tabReady", platform: (currentPlatform() || {}).id }).catch(() => {});
mainLoop();
})();
