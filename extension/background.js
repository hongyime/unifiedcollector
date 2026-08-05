// UnifiedCollector Social Bridge — background service worker (MV3).
//
// MV3 service workers are EPHEMERAL: Chrome sleeps them after ~30s idle. So we
// don't run a persistent loop. Instead scraping is EVENT-DRIVEN:
//   • a scraper tab loads / becomes active  -> kick a cycle right away
//   • a short heartbeat alarm                -> kick a cycle, but ONLY while a
//                                               scraper tab is open
//   • no scraper tab open                    -> do nothing (graceful pause)
//   • throttle wall hit                      -> global cooldown (back off)
// This file also keeps a persistent, storage-backed LOG ring buffer so the popup
// shows recent activity even after the worker slept and respawned.

// Global crash sinks: if any listener throws async or the top-level IIFE below
// rejects, MV3 logs "Service worker went to a bad state unexpectedly" and gives
// up. Capture both here so the next time it happens we have a stack in ucLog
// AND on the ingest backend, instead of an anonymous ":0" frame in
// chrome://extensions.
self.addEventListener("error", (event) => {
  const detail = {
    kind: "sw_error_event",
    message: (event && event.message) || null,
    filename: (event && event.filename) || null,
    lineno: (event && event.lineno) || null,
    colno: (event && event.colno) || null,
    stack: (event && event.error && event.error.stack) || null,
  };
  try { console.error("[UC sw_error]", detail); } catch (_) {}
  try { _reportSwCrash(detail); } catch (_) {}
});
self.addEventListener("unhandledrejection", (event) => {
  const reason = event && event.reason;
  const detail = {
    kind: "sw_unhandled_rejection",
    message: (reason && reason.message) || (typeof reason === "string" ? reason : null),
    stack: (reason && reason.stack) || null,
  };
  try { console.error("[UC sw_reject]", detail); } catch (_) {}
  try { _reportSwCrash(detail); } catch (_) {}
});
function _reportSwCrash(detail) {
  // Fire-and-forget: never throw from the reporter or we'll re-enter the error
  // handler. Both storage write and network POST are best-effort.
  try {
    chrome.storage.local.get("ucLog").then(({ ucLog = [] }) => {
      ucLog.push({ t: Date.now(), level: "error", msg: "sw_crash", detail });
      while (ucLog.length > 200) ucLog.shift();
      chrome.storage.local.set({ ucLog }).catch(() => {});
    }).catch(() => {});
  } catch (_) {}
  try {
    fetch("http://127.0.0.1:8765/social/sw-crash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...detail,
        extension_version: (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || null,
        reported_at: Date.now(),
      }),
      keepalive: true,
    }).catch(() => {});
  } catch (_) {}
}

// Keep the service worker boot path self-contained. A failed importScripts()
// during MV3 startup prevents every alarm/listener from registering, so the
// background worker owns its platform registry directly. Popup/tabs pages still
// load platforms.js for browser UI.
globalThis.UC_PLATFORMS = [
  { id: "instagram", label: "Instagram",   url: "https://www.instagram.com/",       host: "www.instagram.com",  cookieUrl: "https://www.instagram.com",      cookie: "sessionid",  scraper: true, extraUrls: ["https://www.instagram.com/direct/inbox/"] },
  { id: "threads",   label: "Threads",     url: "https://www.threads.com/",         host: "www.threads.com",    cookieUrl: "https://www.threads.com",        cookie: "sessionid",  scraper: true },
  { id: "tiktok",    label: "TikTok",      url: "https://www.tiktok.com/following", host: "www.tiktok.com",     cookieUrl: "https://www.tiktok.com",         cookie: "sessionid",  scraper: true, extraUrls: ["https://www.tiktok.com/foryou", "https://www.tiktok.com/explore"] },
  // Lemon8's SPA renders "Not found" for /feed/<cat> and legacy paths as of
  // 2026-08-05 — single-segment paths (/foryou, /discover, /explore) get
  // treated as usernames (redirected to /@handle) and 404 too. Verified
  // working feed URLs are /topic/<slug>?region=<cc>. See platforms.js for
  // the same override applied to popup/tabs UI.
  { id: "lemon8",    label: "Lemon8",      url: "https://www.lemon8-app.com/topic/food?region=sg", host: "www.lemon8-app.com", cookieUrl: "https://www.lemon8-app.com",     cookie: "sessionid",  scraper: true, noLogin: true },
  { id: "x",         label: "Twitter / X", url: "https://x.com/home",               host: "x.com",              aliasHosts: ["twitter.com"], cookieUrl: "https://x.com",                  cookie: "auth_token", scraper: true },
  { id: "facebook",  label: "Facebook",    url: "https://www.facebook.com/",        host: "www.facebook.com",   cookieUrl: "https://www.facebook.com",       cookie: "c_user",     scraper: true },
  { id: "strava",    label: "Strava",      url: "https://www.strava.com/dashboard", host: "www.strava.com",     cookieUrl: "https://www.strava.com",         cookie: "_strava4_session", scraper: true },
];

const ALARM = "uc-scrape";
const DEFAULT_INGEST = "http://127.0.0.1:8765";
const DEFAULT_CONTROL = "http://127.0.0.1:8700";
const LOG_KEY = "ucLog";
const LOG_MAX = 200;
const WATCHDOG_MIN = 7;          // re-nudge any open scraper tab whose loop died
const KICK_DEBOUNCE_MS = 30000;  // don't re-nudge the same tab more often than this
const BROWSER_UPLOAD_MAX_BYTES = 256 * 1024 * 1024;
const BROWSER_UPLOAD_FETCH_TIMEOUT_MS = 45000;
const BROWSER_UPLOAD_POST_TIMEOUT_MS = 30000;
const BROWSER_UPLOAD_ATTEMPT_LIMIT_BY_PLATFORM = {
  instagram: 3,
  threads: 2,
  tiktok: 1,
  facebook: 1,
  x: 1,
  lemon8: 1,
};
const browserUploadChains = {};
const PAGE_RECOVERY_PREFIX = "uc-page-recovery:";
const PAGE_RECOVERY_STATE_KEY = "ucPageRecovery";
const PAGE_RECOVERY_MAX_ATTEMPTS = 3;
const PAGE_RECOVERY_STATE_TTL_MS = 60 * 60 * 1000;
const PAGE_RECOVERY_LIMIT_COOLDOWN_MS = 30 * 60 * 1000;
const PAGE_RECOVERY_LIMIT_COOLDOWN_MS_BY_PLATFORM = {
  x: 10 * 60 * 1000,
};
const RELOAD_INTENT_KEY = "ucReloadIntent";
const LOADED_VERSION_KEY = "ucLoadedExtensionVersion";
const SCRAPER_HEARTBEAT_TIMEOUT_MS = 12000;
const SCRAPER_HEARTBEAT_CONCURRENCY = 3;
const PAGE_RECOVERY_DELAY_WINDOWS_MS = [
  [45000, 150000],
  [240000, 480000],
  [720000, 1200000],
];
const REFRESH_AFTER_PROGRAMMATIC_INJECT_PLATFORMS = new Set(["x", "threads"]);
const HOME_NAV_HARD_REFRESH_PLATFORMS = new Set(["x", "threads", "lemon8"]);

// ---- persistent logging --------------------------------------------------
async function log(level, msg) {
  const entry = { t: Date.now(), level, msg };
  try {
    const { [LOG_KEY]: cur = [] } = await chrome.storage.local.get(LOG_KEY);
    cur.push(entry);
    while (cur.length > LOG_MAX) cur.shift();
    await chrome.storage.local.set({ [LOG_KEY]: cur });
  } catch (e) {}
  console.log(`[UC ${level}] ${msg}`);
}
async function setStatus(patch) {
  const { ucStatus = {} } = await chrome.storage.local.get("ucStatus");
  await chrome.storage.local.set({ ucStatus: { ...ucStatus, ...patch } });
}
async function getStatus() {
  const { ucStatus = {} } = await chrome.storage.local.get("ucStatus");
  return ucStatus;
}

// ---- config --------------------------------------------------------------
// Cached copies of the endpoint bases. Chrome.storage.local.get from inside
// the message handler was measurable overhead under high heartbeat load: 7
// content tabs × loopStatus every 20–30s + per-cycle logs all serialized on
// storage.local — the SW frequently could not reply within the content
// script's 15 s loopStatus / 10 s log budget and content fell back to
// content_direct. Reading from a module-scope cache eliminates that hop.
// The values are only writable via popup.js (chrome.storage.local.set), so
// a storage.onChanged listener keeps the cache honest.
let _cachedIngestBase = null;
let _cachedControlBase = null;
async function ingestBase() {
  if (_cachedIngestBase) return _cachedIngestBase;
  const { ingestBase } = await chrome.storage.local.get("ingestBase");
  _cachedIngestBase = ingestBase || DEFAULT_INGEST;
  return _cachedIngestBase;
}
async function controlBase() {
  if (_cachedControlBase) return _cachedControlBase;
  const { controlBase } = await chrome.storage.local.get("controlBase");
  _cachedControlBase = controlBase || DEFAULT_CONTROL;
  return _cachedControlBase;
}
try {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.ingestBase) _cachedIngestBase = changes.ingestBase.newValue || DEFAULT_INGEST;
    if (changes.controlBase) _cachedControlBase = changes.controlBase.newValue || DEFAULT_CONTROL;
  });
} catch (e) { /* addListener unavailable in some contexts */ }
function extensionVersion() {
  return (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || null;
}
function withExtensionVersion(payload) {
  return { ...payload, extension_version: extensionVersion() };
}
async function postJsonWithTimeout(url, payload, timeoutMs = 10000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    });
    const body = await r.text().catch(() => "");
    if (!r.ok) throw new Error(`HTTP ${r.status}${body ? ": " + body.slice(0, 180) : ""}`);
    return { response: r, body };
  } finally {
    clearTimeout(timer);
  }
}
async function reportBridgeHeartbeat(reason) {
  const base = await ingestBase();
  const payload = withExtensionVersion({
    platform: "bridge",
    label: "UnifiedCollector Bridge",
    running: true,
    tab_id: "service_worker",
    url: "chrome-extension://" + chrome.runtime.id + "/background.js",
    health_status: "service_worker_active",
    health_reason: reason || "startup",
  });
  try {
    const result = await postJsonWithTimeout(base + "/social/browser-heartbeat", payload);
    const body = parseHeartbeatResponseBody(result && result.body);
    await maybeAutoReloadExtension(
      base,
      null,
      { id: "bridge", label: "UnifiedCollector Bridge" },
      body,
      reason || "bridge_heartbeat",
    );
    await setStatus({
      lastBridgeHeartbeatOkAt: Date.now(),
      lastBridgeHeartbeatError: null,
      lastBridgeHeartbeatBase: base,
      lastBridgeHeartbeatReason: reason || "startup",
    });
  } catch (e) {
    const err = String(e && e.message ? e.message : e);
    await setStatus({
      lastBridgeHeartbeatFailAt: Date.now(),
      lastBridgeHeartbeatError: err,
      lastBridgeHeartbeatBase: base,
      lastBridgeHeartbeatReason: reason || "startup",
    });
    await log("warn", `browser heartbeat failed to reach ${base}: ${err}`);
  }
}
async function sendManualIngestProbe() {
  const base = await ingestBase();
  const payload = withExtensionVersion({
    platform: "bridge",
    label: "UnifiedCollector Bridge",
    running: true,
    tab_id: "manual_test",
    url: "chrome-extension://" + chrome.runtime.id + "/tabs.html",
    health_status: "manual_ingest_probe",
    health_reason: "tabs_page_test",
  });
  try {
    const { response: r } = await postJsonWithTimeout(base + "/social/browser-heartbeat", payload, 6000);
    await setStatus({ lastManualIngestProbeAt: Date.now(), lastManualIngestProbeOk: true, lastManualIngestProbeError: null });
    await log("info", `manual ingest probe ok (${base})`);
    return { ok: true, status: r.status, base };
  } catch (e) {
    const err = String(e && e.message ? e.message : e);
    await setStatus({ lastManualIngestProbeAt: Date.now(), lastManualIngestProbeOk: false, lastManualIngestProbeError: err });
    await log("error", `manual ingest probe failed: ${err}`);
    return { ok: false, error: err, base };
  }
}
function platformForTabUrl(url) {
  let host = "";
  try { host = new URL(url || "").host; } catch (e) { return null; }
  return scraperPlatforms().find((p) => platformHosts(p).includes(host)) || null;
}
async function reportScraperTabHeartbeats(reason) {
  const base = await ingestBase();
  let seen = 0;
  let sent = 0;
  let failed = 0;
  let lastError = null;
  let canonical = 0;
  let skipped = 0;
  try {
    const allTabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
    const selection = selectCanonicalScraperTabRows(allTabs);
    const tabs = selection.rows;
    seen = selection.seen;
    canonical = tabs.length;
    skipped = selection.skipped;
    await mapLimit(tabs, SCRAPER_HEARTBEAT_CONCURRENCY, async ({ tab, platform }) => {
      try {
        const result = await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
          platform: platform.id,
          label: platform.label,
          running: true,
          tab_id: tab.id,
          url: tab.url || null,
          health_status: "background_tab_seen",
          health_reason: reason || "background_watchdog",
          page_title: tab.title || null,
        }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
        await scheduleMaybeForceScrapeCycle(tab, platform, result && result.body, reason || "background_watchdog", base);
        sent++;
      } catch (e) {
        failed++;
        lastError = String(e && e.message ? e.message : e);
      }
    });
  } catch (e) {
    lastError = String(e && e.message ? e.message : e);
  }
  await setStatus({
    lastScraperHeartbeatAt: Date.now(),
    lastScraperHeartbeatSeen: seen,
    lastScraperHeartbeatSent: sent,
    lastScraperHeartbeatFailed: failed,
    lastScraperHeartbeatCanonical: canonical,
    lastScraperHeartbeatSkippedDuplicates: skipped,
    lastScraperHeartbeatError: lastError,
    lastScraperHeartbeatBase: base,
    lastScraperHeartbeatReason: reason || "background_watchdog",
  });
  if (failed || lastError) {
    await log("warn", `scraper heartbeat delivery: ${sent}/${seen} sent to ${base}${lastError ? " (" + lastError + ")" : ""}`);
  }
  await reportScraperHeartbeatSummary(base, reason, { seen, sent, failed, canonical, skipped, lastError })
    .catch(() => {});
}

async function reportScraperHeartbeatSummary(base, reason, summary) {
  const seen = Number(summary && summary.seen || 0);
  const sent = Number(summary && summary.sent || 0);
  const failed = Number(summary && summary.failed || 0);
  const lastError = summary && summary.lastError ? String(summary.lastError).slice(0, 240) : null;
  const healthStatus = seen <= 0
    ? "scraper_tabs_missing"
    : failed > 0 || sent <= 0
      ? "scraper_heartbeat_degraded"
      : "scraper_heartbeat_ok";
  await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
    platform: "bridge",
    label: "UnifiedCollector Bridge",
    running: true,
    tab_id: "scraper_tabs",
    url: "chrome-extension://" + chrome.runtime.id + "/background.js",
    health_status: healthStatus,
    health_reason: reason || "background_watchdog",
    scraper_tabs_seen: seen,
    scraper_tabs_sent: sent,
    scraper_tabs_failed: failed,
    scraper_tabs_canonical: Number(summary && summary.canonical || 0),
    scraper_tabs_skipped: Number(summary && summary.skipped || 0),
    scraper_heartbeat_error: lastError,
  }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
}

const lastForcedCycleByTab = {};
const lastForcedReloadByTab = {};
const lastForcedFailureByTab = {};
const FORCED_CYCLE_DEBOUNCE_MS = 5 * 60 * 1000;
const FORCED_CYCLE_HARD_RELOAD_MS_BY_PLATFORM = {
  x: 5 * 60 * 1000,
  facebook: 7 * 60 * 1000,
  tiktok: 6 * 60 * 1000,
  lemon8: 6 * 60 * 1000,
  threads: 6 * 60 * 1000,
  strava: 7 * 60 * 1000,
  instagram: 12 * 60 * 1000,
};
const FORCED_CYCLE_RELOAD_DEBOUNCE_MS = 4 * 60 * 1000;
const FORCED_CYCLE_FAILURE_DEBOUNCE_MS = 90 * 1000;
const TAB_MESSAGE_TIMEOUT_MS = 30000;
const MESSAGE_TIMEOUT_STALE_RELOAD_SECONDS_BY_PLATFORM = {
  facebook: 10 * 60,
  instagram: 10 * 60,
  lemon8: 10 * 60,
  x: 10 * 60,
  tiktok: 10 * 60,
  threads: 10 * 60,
};
const EXTENSION_AUTO_RELOAD_STATE_KEY = "ucExtensionAutoReloadState";
const EXTENSION_AUTO_RELOAD_DEBOUNCE_MS = 2 * 60 * 1000;
const EXTENSION_AUTO_RELOAD_DELAY_MS = 1200;
const EXTENSION_AUTO_RELOAD_MAX_ATTEMPTS = 2;
const EXTENSION_AUTO_RELOAD_ALARM = "uc-extension-auto-reload";

function forcedCycleHardReloadMs(platformId) {
  return FORCED_CYCLE_HARD_RELOAD_MS_BY_PLATFORM[platformId] || 5 * 60 * 1000;
}

function hardRefreshNavigationUrl(platform, currentUrl, now) {
  if (!platform || !HOME_NAV_HARD_REFRESH_PLATFORMS.has(platform.id)) return null;
  const current = String(currentUrl || "");
  if (platform.id === "x") {
    try {
      const u = new URL(current || platform.url || "https://x.com/home");
      if (/twitter\.com$/i.test(u.hostname)) return "https://x.com/home";
      return "https://twitter.com/home";
    } catch (e) {
      return "https://twitter.com/home";
    }
  }
  if (platform.id === "threads") {
    return `https://www.threads.com/?uc_recover=${Math.floor(now / 1000)}`;
  }
  return platform.url && current !== platform.url ? platform.url : null;
}

function isNoReceiverError(err) {
  return /Could not establish connection|Receiving end does not exist|Extension context invalidated/i.test(
    String((err && err.message) || err || "")
  );
}

function isTabMessageTimeout(err) {
  return /tab message timed out/i.test(String((err && err.message) || err || ""));
}

function messageTimeoutStaleReloadSeconds(platformId) {
  return MESSAGE_TIMEOUT_STALE_RELOAD_SECONDS_BY_PLATFORM[platformId] || 10 * 60;
}

async function sendTabMessageWithTimeout(tabId, message, timeoutMs = TAB_MESSAGE_TIMEOUT_MS) {
  let timer = null;
  try {
    return await Promise.race([
      chrome.tabs.sendMessage(tabId, message),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("tab message timed out")), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function recordServiceWorkerRecovery(base, tab, platform, status, reason, extra = {}) {
  if (!base || !platform || !platform.id) return;
  try {
    await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
      platform: platform.id,
      label: platform.label || platform.id,
      running: true,
      tab_id: tab && tab.id != null ? tab.id : null,
      url: tab && tab.url ? tab.url : null,
      page_title: tab && tab.title ? tab.title : null,
      health_status: status,
      health_reason: reason || null,
      service_worker_recovery: true,
      ...extra,
    }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
  } catch (e) {}
}

async function injectContentScriptAndNudge(base, tab, platform, reason, extra = {}) {
  if (!tab || tab.id == null || !platform || !platform.id) return false;
  const tabId = tab.id;
  let freshTab = tab;
  try {
    freshTab = await chrome.tabs.get(tabId);
  } catch (e) {}
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });
    await recordServiceWorkerRecovery(base, freshTab, platform, "content_script_programmatic_injected", reason || "content_script_receiver_missing", extra);
  } catch (e) {
    await recordServiceWorkerRecovery(base, freshTab, platform, "content_script_programmatic_inject_failed", reason || "content_script_receiver_missing", {
      ...extra,
      inject_error: e && e.message ? e.message : String(e),
    });
    return false;
  }
  try {
    await sendTabMessageWithTimeout(tabId, {
      type: "scrapeCycle",
      reason: reason || "content_script_receiver_missing",
    });
    await recordServiceWorkerRecovery(base, freshTab, platform, "content_script_programmatic_nudge_sent", reason || "content_script_receiver_missing", extra);
    return true;
  } catch (e) {
    const messageTimedOut = isTabMessageTimeout(e);
    await recordServiceWorkerRecovery(base, freshTab, platform, "content_script_programmatic_nudge_failed", reason || "content_script_receiver_missing", {
      ...extra,
      cycle_error: e && e.message ? e.message : String(e),
      message_timeout: messageTimedOut || null,
    });
    if (messageTimedOut) {
      await recordServiceWorkerRecovery(base, freshTab, platform, "content_script_programmatic_nudge_timed_out", reason || "content_script_receiver_missing", {
        ...extra,
        cycle_error: e && e.message ? e.message : String(e),
        message_timeout: true,
      });
      return true;
    }
    return false;
  }
}

const POST_RELOAD_NUDGE_DELAY_MS_BY_PLATFORM = {
  facebook: 30000,
  instagram: 25000,
  lemon8: 25000,
  threads: 45000,
  tiktok: 75000,
  x: 75000,
};
const POST_RELOAD_NUDGE_RETRY_DELAY_MS_BY_PLATFORM = {
  threads: 60000,
  tiktok: 90000,
  x: 90000,
};

function postReloadScrapeNudgeDelayMs(platform, extra = {}) {
  if (Number.isFinite(Number(extra.post_reload_delay_ms)) && Number(extra.post_reload_delay_ms) > 0) {
    return Number(extra.post_reload_delay_ms);
  }
  return POST_RELOAD_NUDGE_DELAY_MS_BY_PLATFORM[platform && platform.id] || 12000;
}
function postReloadScrapeNudgeRetryDelayMs(platform) {
  return POST_RELOAD_NUDGE_RETRY_DELAY_MS_BY_PLATFORM[platform && platform.id] || 45000;
}

function schedulePostReloadScrapeNudge(base, tab, platform, reason, extra = {}) {
  if (!tab || tab.id == null || !platform || !platform.id) return;
  const tabId = tab.id;
  const delayMs = postReloadScrapeNudgeDelayMs(platform, extra);
  setTimeout(() => {
    (async () => {
      let freshTab = tab;
      try {
        freshTab = await chrome.tabs.get(tabId);
      } catch (e) {}
      try {
        await sendTabMessageWithTimeout(tabId, {
          type: "scrapeCycle",
          reason: reason || "post_reload_recovery",
        });
        await recordServiceWorkerRecovery(base, freshTab, platform, "post_reload_scrape_nudge_sent", reason || "post_reload_recovery", {
          ...extra,
          post_reload_delay_ms: delayMs,
        });
      } catch (e) {
        const messageTimedOut = isTabMessageTimeout(e);
        if (isNoReceiverError(e)) {
          const injected = await injectContentScriptAndNudge(base, freshTab, platform, reason || "post_reload_recovery", {
            ...extra,
            post_reload_delay_ms: delayMs,
            recovery: "post_reload_programmatic_inject",
          });
          if (injected) return;
        }
        if (messageTimedOut && !extra.post_reload_retry) {
          const retryDelayMs = postReloadScrapeNudgeRetryDelayMs(platform);
          await recordServiceWorkerRecovery(base, freshTab, platform, "post_reload_scrape_nudge_retry_scheduled", reason || "post_reload_recovery", {
            ...extra,
            cycle_error: e && e.message ? e.message : String(e),
            message_timeout: true,
            post_reload_delay_ms: delayMs,
            post_reload_retry_delay_ms: retryDelayMs,
          });
          schedulePostReloadScrapeNudge(base, freshTab, platform, reason || "post_reload_recovery", {
            ...extra,
            post_reload_retry: true,
            post_reload_delay_ms: retryDelayMs,
          });
          return;
        }
        if (messageTimedOut) {
          const injected = await injectContentScriptAndNudge(base, freshTab, platform, reason || "post_reload_recovery", {
            ...extra,
            cycle_error: e && e.message ? e.message : String(e),
            message_timeout: true,
            post_reload_delay_ms: delayMs,
            recovery: "post_reload_timeout_programmatic_inject",
          });
          if (injected) return;
        }
        await recordServiceWorkerRecovery(base, freshTab, platform, "post_reload_scrape_nudge_failed", reason || "post_reload_recovery", {
          ...extra,
          cycle_error: e && e.message ? e.message : String(e),
          message_timeout: messageTimedOut || null,
          post_reload_delay_ms: delayMs,
        });
      }
    })().catch(() => {});
  }, delayMs);
}

async function hardRefreshForcedCycleTab(base, tab, platform, reason, extra = {}) {
  if (!tab || tab.id == null || !platform || !platform.id) return false;
  const now = Date.now();
  const key = String(tab.id);
  const lastReloadAt = Number(lastForcedReloadByTab[key] || 0);
  if (lastReloadAt && now - lastReloadAt < FORCED_CYCLE_RELOAD_DEBOUNCE_MS) {
    await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_reload_skipped", "reload_debounce", {
      reload_age_ms: now - lastReloadAt,
      ...extra,
    });
    return false;
  }
  const currentUrl = tab && tab.url ? String(tab.url) : "";
  const targetUrl = hardRefreshNavigationUrl(platform, currentUrl, now);
  await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_hard_refresh", reason || "forced_cycle_recovery", {
    ...extra,
    recovery_nav: targetUrl ? "home_url" : "reload",
    recovery_target_url: targetUrl || null,
  });
  if (targetUrl) {
    await chrome.tabs.update(tab.id, { url: targetUrl });
  } else {
    await chrome.tabs.reload(tab.id, { bypassCache: true });
  }
  lastForcedReloadByTab[key] = now;
  lastForcedCycleByTab[key] = 0;
  schedulePostReloadScrapeNudge(base, tab, platform, reason || "forced_cycle_recovery", extra);
  return true;
}

async function refreshTabForMissingContentScript(base, tab, platform, reason, extra = {}) {
  if (!tab || tab.id == null || !platform || !platform.id) return false;
  const injected = await injectContentScriptAndNudge(base, tab, platform, reason || "content_script_receiver_missing", {
    recovery: "receiver_missing_programmatic_inject",
    ...extra,
  });
  if (injected) {
    if (REFRESH_AFTER_PROGRAMMATIC_INJECT_PLATFORMS.has(platform.id)) {
      await recordServiceWorkerRecovery(base, tab, platform, "content_script_injected_refresh", reason || "content_script_receiver_missing", {
        recovery: "receiver_missing_injected_then_refresh",
        ...extra,
      });
      return hardRefreshForcedCycleTab(base, tab, platform, reason || "content_script_receiver_missing", {
        recovery: "receiver_missing_injected_then_refresh",
        ...extra,
      });
    }
    return true;
  }
  // If programmatic injection still cannot attach, reload the page so Chrome can
  // run the manifest content script in a clean page context.
  return hardRefreshForcedCycleTab(base, tab, platform, reason || "content_script_receiver_missing", {
    recovery: "manifest_content_script_refresh",
    ...extra,
  });
}

function parseHeartbeatResponseBody(responseBody) {
  if (!responseBody) return null;
  if (typeof responseBody === "object") return responseBody;
  try { return JSON.parse(String(responseBody)); } catch (e) { return null; }
}

function normalizeExtensionVersion(value) {
  return String(value || "").trim().replace(/^v/i, "");
}

async function maybeAutoReloadExtension(base, tab, platform, body, reason) {
  if (!body || body.reload_extension !== true) return false;
  const expected = normalizeExtensionVersion(body.expected_extension_version);
  const current = normalizeExtensionVersion(extensionVersion());
  if (!expected || !current || expected === current) return false;

  const now = Date.now();
  const stored = await chrome.storage.local.get(EXTENSION_AUTO_RELOAD_STATE_KEY);
  const state = stored[EXTENSION_AUTO_RELOAD_STATE_KEY] || {};
  const attempts = state.expected === expected ? Number(state.attempts || 0) : 0;
  const lastAt = state.expected === expected ? Number(state.last_at || 0) : 0;
  const reloadReason = body.reload_reason || reason || "extension_version_mismatch";

  if (attempts >= EXTENSION_AUTO_RELOAD_MAX_ATTEMPTS) {
    await recordServiceWorkerRecovery(base, tab, platform, "extension_auto_reload_gave_up", reloadReason, {
      expected_extension_version: expected,
      current_extension_version: current,
      reload_attempts: attempts,
      reload_max_attempts: EXTENSION_AUTO_RELOAD_MAX_ATTEMPTS,
    });
    return false;
  }
  if (lastAt && now - lastAt < EXTENSION_AUTO_RELOAD_DEBOUNCE_MS) {
    await recordServiceWorkerRecovery(base, tab, platform, "extension_auto_reload_debounced", reloadReason, {
      expected_extension_version: expected,
      current_extension_version: current,
      reload_attempts: attempts,
      reload_age_ms: now - lastAt,
    });
    return true;
  }

  const nextAttempts = attempts + 1;
  await chrome.storage.local.set({
    [EXTENSION_AUTO_RELOAD_STATE_KEY]: {
      expected,
      attempts: nextAttempts,
      last_at: now,
    },
  });
  await recordServiceWorkerRecovery(base, tab, platform, "extension_auto_reload_scheduled", reloadReason, {
    expected_extension_version: expected,
    current_extension_version: current,
    reload_attempt: nextAttempts,
    reload_delay_ms: EXTENSION_AUTO_RELOAD_DELAY_MS,
  });
  await log("warn", `extension ${current} is older than expected ${expected}; reloading extension`);
  try {
    await chrome.alarms.create(EXTENSION_AUTO_RELOAD_ALARM, { when: Date.now() + EXTENSION_AUTO_RELOAD_DELAY_MS });
  } catch (e) {}
  try { chrome.runtime.reload(); } catch (e) {}
  return true;
}

async function maybeForceScrapeCycle(tab, platform, responseBody, reason, base = null) {
  const body = parseHeartbeatResponseBody(responseBody);
  if (!body) return;
  const reloadScheduled = await maybeAutoReloadExtension(base, tab, platform, body, reason);
  if (reloadScheduled || body.force_cycle !== true || !tab || !tab.id) return;
  const now = Date.now();
  const key = String(tab.id);
  const lastForcedAt = Number(lastForcedCycleByTab[key] || 0);
  const forcedAgeMs = lastForcedAt ? now - lastForcedAt : 0;
  const hardReloadMs = forcedCycleHardReloadMs(platform && platform.id);
  if (lastForcedAt && forcedAgeMs > hardReloadMs) {
    const lastReloadAt = Number(lastForcedReloadByTab[key] || 0);
    if (!lastReloadAt || now - lastReloadAt > FORCED_CYCLE_RELOAD_DEBOUNCE_MS) {
      try {
        await hardRefreshForcedCycleTab(base, tab, platform, "stale_forced_cycle", {
          forced_age_ms: forcedAgeMs,
          hard_reload_ms: hardReloadMs,
          content_age_seconds: body.content_age_seconds || null,
        });
        await log("warn", `${platform.label}: stale forced scrape did not finish after ${Math.round(forcedAgeMs / 1000)}s; hard-refreshed tab`);
      } catch (e) {
        await log("warn", `${platform.label}: stale forced scrape hard-refresh failed: ${e && e.message ? e.message : e}`);
      }
      return;
    }
  }
  if (lastForcedAt && forcedAgeMs < FORCED_CYCLE_DEBOUNCE_MS) return;
  const recoveryMessage = {
    type: "scrapeCycle",
    reason: body.force_reason || "browser_content_stale",
    content_age_seconds: body.content_age_seconds || null,
  };
  try {
    lastForcedCycleByTab[key] = now;
    await sendTabMessageWithTimeout(tab.id, recoveryMessage);
    await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_sent", body.force_reason || "browser_content_stale", {
      content_age_seconds: body.content_age_seconds || null,
      message_type: recoveryMessage.type,
    });
    await log("warn", `${platform.label}: nudged scraper loop because browser content is stale`);
  } catch (firstErr) {
    const cycleError = firstErr && firstErr.message ? firstErr.message : String(firstErr);
    const failKey = `${key}:${platform && platform.id ? platform.id : "unknown"}`;
    const lastFailureAt = Number(lastForcedFailureByTab[failKey] || 0);
    const failureAgeMs = lastFailureAt ? Date.now() - lastFailureAt : 0;
    const receiverMissing = isNoReceiverError(firstErr);
    const messageTimedOut = isTabMessageTimeout(firstErr);
    if (receiverMissing && lastFailureAt && failureAgeMs < FORCED_CYCLE_FAILURE_DEBOUNCE_MS) {
      await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_debounced", body.force_reason || "browser_content_stale", {
        content_age_seconds: body.content_age_seconds || null,
        cycle_error: cycleError,
        failure_age_ms: failureAgeMs,
        no_receiver: true,
      });
      return;
    }
    lastForcedFailureByTab[failKey] = Date.now();
    if (messageTimedOut) {
      const contentAgeSeconds = Number(body.content_age_seconds || 0);
      const staleReloadSeconds = messageTimeoutStaleReloadSeconds(platform && platform.id);
      if (Number.isFinite(contentAgeSeconds) && contentAgeSeconds >= staleReloadSeconds) {
        const reloaded = await hardRefreshForcedCycleTab(base, tab, platform, "message_timeout_content_stale", {
          content_age_seconds: contentAgeSeconds,
          stale_reload_seconds: staleReloadSeconds,
          cycle_error: cycleError,
          message_timeout: true,
          recovery: "message_timeout_stale_refresh",
        });
        await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_timed_out", body.force_reason || "browser_content_stale", {
          content_age_seconds: contentAgeSeconds,
          stale_reload_seconds: staleReloadSeconds,
          cycle_error: cycleError,
          message_timeout: true,
          stale_refresh_attempted: true,
          stale_refresh_ok: reloaded || null,
        });
        if (!reloaded) {
          const injected = await injectContentScriptAndNudge(base, tab, platform, "forced_cycle_reload_debounced_timeout", {
            content_age_seconds: contentAgeSeconds,
            stale_reload_seconds: staleReloadSeconds,
            cycle_error: cycleError,
            message_timeout: true,
            recovery: "reload_debounced_programmatic_inject",
          });
          await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_reload_debounced_inject", body.force_reason || "browser_content_stale", {
            content_age_seconds: contentAgeSeconds,
            stale_reload_seconds: staleReloadSeconds,
            cycle_error: cycleError,
            message_timeout: true,
            reinject_attempted: true,
            reinject_ok: injected || null,
          });
        }
        await log("warn", `${platform.label}: stale-content forced scrape message timed out; ${reloaded ? "hard-refreshed stale tab" : "refresh skipped by debounce"}`);
        return;
      }
      const injected = await injectContentScriptAndNudge(base, tab, platform, "forced_cycle_message_timeout", {
        content_age_seconds: body.content_age_seconds || null,
        cycle_error: cycleError,
        message_timeout: true,
        recovery: "message_timeout_programmatic_inject",
      });
      await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_timed_out", body.force_reason || "browser_content_stale", {
        content_age_seconds: body.content_age_seconds || null,
        cycle_error: cycleError,
        message_timeout: true,
        reinject_attempted: true,
        reinject_ok: injected || null,
      });
      await log("warn", `${platform.label}: stale-content forced scrape message timed out; ${injected ? "re-injected content script and left tab running" : "leaving the tab running and retrying later"}`);
      return;
    }
    await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_failed", body.force_reason || "browser_content_stale", {
      content_age_seconds: body.content_age_seconds || null,
      cycle_error: cycleError,
      no_receiver: receiverMissing || null,
    });
    try {
      if (receiverMissing) {
        try {
          const reloaded = await refreshTabForMissingContentScript(base, tab, platform, "content_script_receiver_missing", {
            content_age_seconds: body.content_age_seconds || null,
            cycle_error: cycleError,
            no_receiver: true,
          });
          if (reloaded) {
            await log("warn", `${platform.label}: content script receiver missing; hard-refreshed tab so manifest content.js can reload`);
            return;
          }
        } catch (refreshErr) {
          await recordServiceWorkerRecovery(base, tab, platform, "content_script_refresh_failed", "content_script_receiver_missing", {
            content_age_seconds: body.content_age_seconds || null,
            cycle_error: cycleError,
            refresh_error: refreshErr && refreshErr.message ? refreshErr.message : String(refreshErr),
            no_receiver: true,
          });
        }
      }
      const reloaded = await hardRefreshForcedCycleTab(base, tab, platform, receiverMissing ? "content_script_receiver_missing" : "failed_force_message", {
        content_age_seconds: body.content_age_seconds || null,
        cycle_error: cycleError,
        no_receiver: receiverMissing || null,
      });
      if (reloaded) {
        await log("warn", `${platform.label}: stale-content forced scrape failed; hard-refreshed tab (${cycleError})`);
      } else {
        await log("warn", `${platform.label}: stale-content forced scrape failed; reload already debounced (${cycleError})`);
      }
    } catch (reloadErr) {
      await log("warn", `${platform.label}: stale-content forced scrape failed and hard-refresh failed: ${reloadErr && reloadErr.message ? reloadErr.message : reloadErr}`);
    }
  }
}

async function scheduleMaybeForceScrapeCycle(tab, platform, responseBody, reason, base = null) {
  try {
    await maybeForceScrapeCycle(tab, platform, responseBody, reason, base);
  } catch (e) {
    await log("warn", `${platform && platform.label ? platform.label : "scraper"} recovery failed: ${e && e.message ? e.message : e}`);
  }
}
async function fetchJsonWithTimeout(url, timeoutMs = 12000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ctrl.signal });
    const j = await r.json().catch(() => ({}));
    return { response: r, json: j };
  } finally {
    clearTimeout(timer);
  }
}

// ---- watchdog + AUTO-TABS (open + refresh) --------------------------------
// The 83h-stall problem: a closed/orphaned tab = no scraping. So the worker now
// auto-OPENS every scraper tab (pinned, background) and auto-REFRESHES them
// hourly — reloading respawns the content script + loop AND pulls fresh content,
// so it can never silently die again.
const ALARM_REFRESH = "uc-refresh";
const REFRESH_MIN = 45;

// ---- memory-driven soft reload -------------------------------------------
// Facebook (and threads to a lesser extent) accumulate DOM nodes from infinite
// scroll — baseline 2026-08-05 showed the FB tab at 260MB JS heap / 18k DOM
// elements / 32k Blink nodes / 27k LayoutObjects after just a few hours; the
// tab's untouched growth path leads to Chrome killing it OOM. Every kill costs
// re-login on an unlucky cookie flush and leaves the tab group short a member.
// The 45-min ALARM_REFRESH cycle already reloads all scraper tabs, but its
// cadence lets FB heap grow well past 250MB between reloads on heavy-feed days
// (staggered reloads mean each tab may wait 90-120s past nominal cadence too).
// We add a THIRD alarm that polls memory-sensitive tabs and soft-reloads once
// the JS heap or DOM-node count crosses a threshold, or once the tab has run
// for MEMORY_TIME_CAP_MIN without a memory-driven reload. `chrome.tabs.reload`
// preserves tab-group membership + pinned state (same primitive
// refreshScraperTabs uses), so this is safe to layer on top of the existing
// refresh loop.
const ALARM_MEMORY = "uc-memory-check";
const MEMORY_SENSITIVE_PLATFORMS = new Set(["facebook", "threads"]);
const MEMORY_CHECK_INTERVAL_MIN = 30;
const MEMORY_RELOAD_THRESHOLD_JS_MB = 250;
const MEMORY_RELOAD_DOM_NODES = 40000;
const MEMORY_RELOAD_MIN_INTERVAL_MIN = 90;
const MEMORY_TIME_CAP_MIN = 240; // 4h safety cap on quiet days
const MEMORY_LAST_RELOAD_KEY = "ucMemoryLastReloadByTab";
const MEMORY_THRESHOLD_MB_OVERRIDE_KEY = "memoryReloadThresholdMB";
const MEMORY_DOM_OVERRIDE_KEY = "memoryReloadDomNodes";

async function autoTabsEnabled() {
  const { ucAutoTabs } = await chrome.storage.local.get("ucAutoTabs");
  return ucAutoTabs !== false; // default ON
}
const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const _jitterMs = (base, spread = 0.55) => Math.round(base * (1 - spread + Math.random() * spread * 2));
const _humanGap = (base) => {
  let ms = _jitterMs(base, 0.65);
  if (Math.random() < 0.12) ms += 3000 + Math.random() * 9000;
  return Math.max(750, Math.round(ms));
};
const _randBetween = (min, max) => Math.round(min + Math.random() * (max - min));
const _alarmJitter = () => _sleep(15000 + Math.random() * 120000);
let _tabsOpInProgress = false; // guard against overlapping open/refresh runs (no spam)
let _startupRecoveryChain = Promise.resolve();

async function mapLimit(items, limit, worker) {
  const out = [];
  let next = 0;
  const runners = Array.from({ length: Math.max(1, Math.min(limit, items.length || 1)) }, async () => {
    while (next < items.length) {
      const index = next++;
      out[index] = await worker(items[index], index);
    }
  });
  await Promise.all(runners);
  return out;
}

// Open exactly the missing scraper tabs — pinned, background, ONE at a time with a
// gap so we never spam tabs or spike CPU. Robust dedup by host+path-prefix means a
// tab is never duplicated.
const _tpath = (u) => { try { return new URL(u).pathname.split("?")[0].replace(/\/$/, "") || "/"; } catch (e) { return "/"; } };
const _mainWorldHookHostRe = /(^|\.)((instagram|tiktok|strava)\.com|threads\.com|x\.com|twitter\.com|facebook\.com)$/i;

function scraperTabRows(tabs) {
  return (tabs || [])
    .map((tab) => ({ tab, platform: platformForTabUrl(tab.url) }))
    .filter((row) => row.platform && row.tab && row.tab.id != null);
}

function platformTargetPaths(platform) {
  if (!platform) return [];
  return [platform.url, ...((platform.extraUrls || []))]
    .filter(Boolean)
    .map(_tpath);
}

function canonicalTabKey(row) {
  const platform = row && row.platform;
  const tab = row && row.tab;
  if (!platform || !tab) return null;
  const targets = platformTargetPaths(platform);
  if (targets.length <= 1) return platform.id;
  const path = _tpath(tab.url || "");
  const matched = targets.find((want) => path === want || (want !== "/" && path.startsWith(want)));
  return `${platform.id}:${matched || path}`;
}

function canonicalTabScore(row) {
  const tab = row && row.tab ? row.tab : {};
  const path = _tpath(tab.url || "");
  const targets = platformTargetPaths(row && row.platform);
  let score = 0;
  if (tab.status === "complete") score += 50;
  if (!tab.discarded) score += 20;
  if (tab.active) score += 10;
  if (tab.pinned) score += 5;
  if (targets.some((want) => path === want)) score += 8;
  else if (targets.some((want) => want !== "/" && path.startsWith(want))) score += 4;
  return score;
}

function selectCanonicalScraperTabRows(tabs) {
  const rows = scraperTabRows(tabs);
  const byKey = new Map();
  let skipped = 0;
  for (const row of rows) {
    const key = canonicalTabKey(row);
    if (!key) continue;
    const cur = byKey.get(key);
    if (!cur) {
      byKey.set(key, row);
      continue;
    }
    const rowScore = canonicalTabScore(row);
    const curScore = canonicalTabScore(cur);
    const rowId = Number(row.tab && row.tab.id);
    const curId = Number(cur.tab && cur.tab.id);
    if (rowScore > curScore || (rowScore === curScore && rowId < curId)) {
      byKey.set(key, row);
    }
    skipped++;
  }
  const selected = Array.from(byKey.values()).sort((a, b) => {
    const ak = canonicalTabKey(a) || "";
    const bk = canonicalTabKey(b) || "";
    return ak.localeCompare(bk);
  });
  return { rows: selected, seen: rows.length, skipped };
}

function shouldNormalizeSingleFeedTab(p, tab, reason) {
  if (!p || !tab || !tab.url) return false;
  // Platforms where a wandering tab (user clicked into a profile, or the SPA
  // routed away from the working feed URL) should snap back to p.url on the
  // next watchdog / startup / install / manual-reload sweep. x is included
  // because failedScript URLs sit in that same "wandered off canonical" bucket;
  // lemon8 is included because the working /topic/<slug> feed is easy to
  // stray from into /@handle / /post/<id> subpaths that have lower yield.
  const NORMALIZE_PLATFORMS = new Set(["x", "lemon8"]);
  if (!NORMALIZE_PLATFORMS.has(p.id)) return false;
  try {
    const u = new URL(tab.url);
    if (u.searchParams.has("failedScript")) return true;
  } catch (e) {
    if (/[?&]failedScript(?:=|&|$)/i.test(String(tab.url || ""))) return true;
  }
  const path = _tpath(tab.url);
  const homePath = _tpath(p.url);
  if (path === homePath || path.startsWith(homePath + "/")) return false;
  return /^(manual_extension_reload|startup|installed|watchdog)$/.test(String(reason || ""));
}

// Chrome tab-group awareness — when the user has grouped their social tabs,
// newly-opened recovery / dedup-replacement tabs should join the same group so
// the layout stays clean. chrome.tabs.query already surfaces groupId on every
// tab under the "tabs" permission (no "tabGroups" permission needed for reads
// or for calling chrome.tabs.group({groupId, tabIds})). Verified empirically
// against Chrome 150 with scripts/probe_tabs_group.py.
async function scraperGroupHint() {
  try {
    const patterns = scraperUrlPatterns();
    if (!patterns.length) return null;
    const tabs = (await chrome.tabs.query({ url: patterns })) || [];
    // Vote by (groupId, windowId) so ties fall on the busiest group in a single
    // window. groupId -1 (TAB_GROUP_ID_NONE) is skipped — un-grouped tabs mean
    // "the user isn't grouping". Tab-group membership is per-window, so we also
    // capture windowId to route the new tab into the correct window.
    const counts = new Map();
    for (const t of tabs) {
      const gid = Number(t.groupId);
      const wid = Number(t.windowId);
      if (!Number.isFinite(gid) || gid < 0 || !Number.isFinite(wid)) continue;
      const key = gid + "|" + wid;
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    if (!counts.size) return null;
    let bestKey = null, bestCount = 0;
    for (const [k, c] of counts.entries()) {
      if (c > bestCount) { bestKey = k; bestCount = c; }
    }
    const [gid, wid] = bestKey.split("|").map(Number);
    return { groupId: gid, windowId: wid };
  } catch (e) { return null; }
}

// Wrap chrome.tabs.create so newly-opened scraper tabs (a) land in the same
// window as the existing social group and (b) immediately join that group.
// Falls back silently to a plain tabs.create if there's no group to join or
// the grouping call fails (e.g. group was deleted mid-call).
async function createTabInSocialGroup(createOptions, hint) {
  const groupHint = hint === undefined ? await scraperGroupHint() : hint;
  const opts = { ...createOptions };
  if (groupHint && Number.isFinite(groupHint.windowId)) opts.windowId = groupHint.windowId;
  const created = await chrome.tabs.create(opts);
  if (groupHint && Number.isFinite(groupHint.groupId) && created && created.id != null) {
    try { await chrome.tabs.group({ tabIds: [created.id], groupId: groupHint.groupId }); }
    catch (e) { /* group vanished or cross-window mismatch — leave tab where it is */ }
  }
  return created;
}

// Keep exactly ONE tab per single-feed platform (instagram/threads/lemon8/x/facebook)
// and one per target path for multi-url platforms (tiktok = foryou + following).
// Closes duplicates so the extension never piles up tabs (the old bug: when the
// auto-opened tab navigated to a sub-path, the dedup missed it and opened another).
async function ensureScraperTabsOpen(reason, options = {}) {
  const force = !!options.force;
  if ((!(await autoTabsEnabled()) && !force) || _tabsOpInProgress) return;
  _tabsOpInProgress = true;
  let opened = 0, closed = 0, navigated = 0;
  // Compute group hint once per sweep — cheap chrome.tabs.query, and reusing
  // it keeps every newly-opened tab pointed at the same group.
  const groupHint = await scraperGroupHint();
  try {
    for (const p of scraperPlatforms()) {
      const tabs = (await chrome.tabs.query({ url: platformUrlPatterns(p) })) || [];
      if (!(p.extraUrls && p.extraUrls.length)) {
        // single-feed: keep one tab on the host, close the rest
        if (tabs.length === 0) {
          try { await createTabInSocialGroup({ url: p.url, pinned: true, active: false }, groupHint); opened++; await _sleep(_humanGap(3000)); } catch (e) {}
        } else {
          if (shouldNormalizeSingleFeedTab(p, tabs[0], reason)) {
            try { await chrome.tabs.update(tabs[0].id, { url: p.url }); navigated++; await _sleep(_humanGap(3000)); } catch (e) {}
          }
          for (let i = 1; i < tabs.length; i++) { try { await chrome.tabs.remove(tabs[i].id); closed++; } catch (e) {} }
        }
      } else {
        // multi-url (tiktok): one tab per target path; close path-duplicates
        const targets = [p.url, ...p.extraUrls];
        const wantPaths = targets.map(_tpath);
        const kept = new Set();
        for (const t of tabs) {
          const tp = _tpath(t.url);
          const m = wantPaths.find((w) => tp === w || (w !== "/" && tp.startsWith(w)));
          if (m) { if (kept.has(m)) { try { await chrome.tabs.remove(t.id); closed++; } catch (e) {} } else kept.add(m); }
        }
        for (let i = 0; i < targets.length; i++) {
          if (!kept.has(wantPaths[i])) { try { await createTabInSocialGroup({ url: targets[i], pinned: true, active: false }, groupHint); opened++; await _sleep(_humanGap(3000)); } catch (e) {} }
        }
      }
    }
    if (opened || closed || navigated) await log("info", `tabs: +${opened} opened, ${closed} dup(s) closed, ${navigated} canonicalized (${reason})`);
  } finally { _tabsOpInProgress = false; }
}

// Reload scraper tabs ONE at a time with a gap (staggered) so the loop respawns
// fresh without reloading 7 tabs simultaneously (CPU spike / overload).
// Push the live, logged-in Instagram cookies to the collector so the HEADLESS
// backup always runs on a fresh session (no dead-cookie 401 retry storms, which
// look bot-like and risk bans). Runs on the refresh cycle + startup.
async function syncCookies() {
  try {
    const cookies = await chrome.cookies.getAll({ domain: "instagram.com" });
    if (!cookies || !cookies.some((c) => c.name === "sessionid")) return; // not logged in
    const dsu = cookies.find((c) => c.name === "ds_user_id");
    const account = dsu ? "live_" + dsu.value : "extension_live";
    const base = await ingestBase();
    const r = await fetch(base + "/social/cookies", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(withExtensionVersion({
        platform: "instagram", account,
        cookies: cookies.map((c) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure, expirationDate: c.expirationDate })),
      })),
    });
    if (r.ok) await log("info", `synced live IG session → headless backup (${account})`);
  } catch (e) { /* cookies perm / ingest down */ }
}

async function refreshScraperTabs(options = {}) {
  if (_tabsOpInProgress) return { ok: false, reason: "tabs_operation_in_progress", reloaded: 0, errors: 0 };
  if (!options.force && !(await autoTabsEnabled())) return { ok: false, reason: "auto_tabs_disabled", reloaded: 0, errors: 0 };
  _tabsOpInProgress = true;
  const bypassCache = !!options.bypassCache;
  const reason = options.reason || "refresh";
  const requestedGapMs = Number(options.gapMs);
  const gapMs = Number.isFinite(requestedGapMs) && requestedGapMs >= 250 ? requestedGapMs : 8000;
  let reloaded = 0;
  let errors = 0;
  try {
    const allTabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
    const selection = selectCanonicalScraperTabRows(allTabs);
    const tabs = selection.rows.map((row) => row.tab);
    for (const t of tabs || []) {
      try {
        await chrome.tabs.reload(t.id, { bypassCache });
        reloaded++;
        await _sleep(_humanGap(gapMs));
      } catch (e) {
        errors++;
      }
    }
    const mode = bypassCache ? "hard-refreshed" : "auto-refreshed";
    await log("info", `${mode} ${tabs ? tabs.length : 0}/${selection.seen} canonical scraper tab(s), skipped ${selection.skipped} duplicate(s), staggered ${gapMs}ms → loop respawns fresh (${reason})`);
    return { ok: true, reloaded, errors, tabs: tabs ? tabs.length : 0, totalTabs: selection.seen, skippedDuplicates: selection.skipped };
  } finally { _tabsOpInProgress = false; }
}

// Fetch live memory + DOM-node counts from a scraper tab via
// `chrome.scripting.executeScript`. Returns null if the tab is discarded,
// nav-blocked, or the injected read failed. `performance.memory` is a
// non-standard Chromium API but works in the content-script isolated world
// and reports the shared process heap; where the FB frame is same-process
// with the extension SW the numbers match Performance.getMetrics.
//
// Facebook (2026-08-05 verification) exposes `performance.memory` only in the
// MAIN world — the isolated world returns undefined. So when isolated-world
// injection returns a null heap, we retry in the MAIN world to catch that
// primary signal. `document.querySelectorAll('*').length` works from either
// world; MAIN-world injection is only for the memory reading.
async function fetchTabMemoryMetrics(tabId) {
  const reader = () => ({
    js_heap_bytes:
      (typeof performance !== "undefined"
        && performance.memory
        && performance.memory.usedJSHeapSize) || null,
    js_heap_limit_bytes:
      (typeof performance !== "undefined"
        && performance.memory
        && performance.memory.jsHeapSizeLimit) || null,
    dom_nodes: document.querySelectorAll("*").length,
  });
  let isolatedResult = null;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: reader,
    });
    if (results && results[0] && results[0].result) isolatedResult = results[0].result;
  } catch (e) {
    return { error: (e && e.message) || String(e) };
  }
  if (isolatedResult && isolatedResult.js_heap_bytes !== null) return isolatedResult;
  // Fall back to MAIN world for sites that lock performance.memory behind
  // the page context (facebook.com verified 2026-08-05).
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: reader,
    });
    if (results && results[0] && results[0].result) {
      const main = results[0].result;
      // Prefer isolated dom_nodes if MAIN failed to read them (matches user's
      // scrape-cycle sampling). Prefer MAIN heap when isolated returned null.
      return {
        js_heap_bytes: main.js_heap_bytes !== null ? main.js_heap_bytes : (isolatedResult && isolatedResult.js_heap_bytes),
        js_heap_limit_bytes: main.js_heap_limit_bytes !== null ? main.js_heap_limit_bytes : (isolatedResult && isolatedResult.js_heap_limit_bytes),
        dom_nodes: main.dom_nodes !== null ? main.dom_nodes : (isolatedResult && isolatedResult.dom_nodes),
      };
    }
  } catch (e) {
    if (isolatedResult) return isolatedResult;
    return { error: (e && e.message) || String(e) };
  }
  return isolatedResult;
}

async function memoryReloadThresholdMB() {
  // Testing override: `chrome.storage.local.set({memoryReloadThresholdMB: N})`.
  try {
    const stored = await chrome.storage.local.get(MEMORY_THRESHOLD_MB_OVERRIDE_KEY);
    const value = Number(stored[MEMORY_THRESHOLD_MB_OVERRIDE_KEY]);
    if (Number.isFinite(value) && value > 0) return value;
  } catch (e) {}
  return MEMORY_RELOAD_THRESHOLD_JS_MB;
}

async function memoryReloadDomThreshold() {
  try {
    const stored = await chrome.storage.local.get(MEMORY_DOM_OVERRIDE_KEY);
    const value = Number(stored[MEMORY_DOM_OVERRIDE_KEY]);
    if (Number.isFinite(value) && value > 0) return value;
  } catch (e) {}
  return MEMORY_RELOAD_DOM_NODES;
}

// Threshold-driven soft reload for memory-sensitive scraper tabs (facebook,
// threads). Called from the ALARM_MEMORY alarm every MEMORY_CHECK_INTERVAL_MIN.
// Reload conditions (any of):
//   - JS heap >= threshold MB (default 250, chrome.storage override supported)
//   - DOM node count >= threshold (default 40k)
//   - time-cap: last memory reload > MEMORY_TIME_CAP_MIN ago (default 4h)
// Never reloads the same tab more often than MEMORY_RELOAD_MIN_INTERVAL_MIN.
// Reload uses chrome.tabs.reload(tabId) which preserves tab-group + pinned
// state; post-reload the standard chrome.tabs.onUpdated -> kick -> ensureLoops
// path respawns the content script, and schedulePostReloadScrapeNudge is
// scheduled as a belt for platforms with a slower warm-up.
async function checkMemorySensitiveTabs(reason = "scheduled") {
  if (_tabsOpInProgress) return { skipped: true, reason: "tabs_operation_in_progress", checked: 0, reloaded: 0 };
  const base = await ingestBase();
  const now = Date.now();
  const jsThresholdMB = await memoryReloadThresholdMB();
  const jsThresholdBytes = jsThresholdMB * 1024 * 1024;
  const domThreshold = await memoryReloadDomThreshold();
  const minIntervalMs = MEMORY_RELOAD_MIN_INTERVAL_MIN * 60 * 1000;
  const timeCapMs = MEMORY_TIME_CAP_MIN * 60 * 1000;
  const stored = await chrome.storage.local.get(MEMORY_LAST_RELOAD_KEY);
  const lastByTab = { ...(stored[MEMORY_LAST_RELOAD_KEY] || {}) };

  let allTabs = [];
  try {
    allTabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
  } catch (e) {
    return { skipped: true, reason: "tabs_query_failed", error: (e && e.message) || String(e), checked: 0, reloaded: 0 };
  }
  const selection = selectCanonicalScraperTabRows(allTabs);
  const rows = selection.rows.filter((r) => r.platform && MEMORY_SENSITIVE_PLATFORMS.has(r.platform.id));

  let checked = 0;
  let reloaded = 0;
  const reasons = [];
  for (const row of rows) {
    const tab = row.tab;
    const platform = row.platform;
    if (!tab || tab.id == null) continue;
    checked++;
    const key = `${platform.id}:${tab.id}`;
    const lastAtStored = Number(lastByTab[key] || 0);
    // Also honor lastForcedReloadByTab so we don't double-reload right after a
    // forced-cycle hard-refresh landed within the debounce window.
    const inMemLastReload = Number(lastForcedReloadByTab[String(tab.id)] || 0);
    const lastAt = Math.max(lastAtStored, inMemLastReload);
    const ageMs = lastAt ? now - lastAt : Number.POSITIVE_INFINITY;

    const metrics = await fetchTabMemoryMetrics(tab.id);
    const jsHeapBytes = metrics && Number.isFinite(Number(metrics.js_heap_bytes)) ? Number(metrics.js_heap_bytes) : null;
    const jsHeapMB = jsHeapBytes !== null ? Number((jsHeapBytes / (1024 * 1024)).toFixed(1)) : null;
    const domNodes = metrics && Number.isFinite(Number(metrics.dom_nodes)) ? Number(metrics.dom_nodes) : null;
    const metricsError = metrics && metrics.error ? String(metrics.error) : null;

    const overHeap = jsHeapBytes !== null && jsHeapBytes >= jsThresholdBytes;
    const overNodes = domNodes !== null && domNodes >= domThreshold;
    // Time-cap only fires after we have established a baseline lastAt in
    // storage — the very first check after install always writes lastAt and
    // waits for the next cycle. This avoids reloading immediately on install
    // when the tab was already open a while.
    const overTimeCap = lastAtStored > 0 && ageMs >= timeCapMs;

    let triggerReason = null;
    if (overHeap) triggerReason = "js_heap_over_threshold";
    else if (overNodes) triggerReason = "dom_nodes_over_threshold";
    else if (overTimeCap) triggerReason = "time_cap_exceeded";

    const baseExtras = {
      memory_js_heap_bytes: jsHeapBytes,
      memory_js_heap_mb: jsHeapMB,
      memory_dom_nodes: domNodes,
      memory_threshold_mb: jsThresholdMB,
      memory_dom_threshold: domThreshold,
      memory_last_reload_age_ms: Number.isFinite(ageMs) ? ageMs : null,
      memory_metrics_error: metricsError,
      memory_check_reason: reason,
    };

    if (!triggerReason) {
      await recordServiceWorkerRecovery(base, tab, platform, "memory_check_skipped", "under_threshold", baseExtras);
      if (!lastAtStored) {
        // Establish a baseline so the time-cap starts counting from the first
        // successful check rather than epoch.
        lastByTab[key] = now;
      }
      continue;
    }

    if (ageMs < minIntervalMs) {
      await recordServiceWorkerRecovery(base, tab, platform, "memory_reload_debounced", triggerReason, baseExtras);
      continue;
    }

    reasons.push(`${platform.id}:${triggerReason}`);
    await recordServiceWorkerRecovery(base, tab, platform, "memory_soft_reload", triggerReason, baseExtras);
    try {
      // bypassCache=false: use HTTP cache so the reload warm-starts faster.
      // tabs.reload preserves tab-group + pinned state (same primitive
      // refreshScraperTabs uses).
      await chrome.tabs.reload(tab.id, { bypassCache: false });
      reloaded++;
      lastByTab[key] = now;
      // Sync with the shared reload-debounce map so other reload paths
      // (forced-cycle recovery etc.) treat this as a fresh reload.
      lastForcedReloadByTab[String(tab.id)] = now;
      await log(
        "warn",
        `${platform.label}: memory soft-reload (${triggerReason}; jsHeap=${jsHeapMB !== null ? jsHeapMB + "MB" : "?"} nodes=${domNodes !== null ? domNodes : "?"})`,
      );
      schedulePostReloadScrapeNudge(base, tab, platform, `memory_soft_reload_${triggerReason}`, {
        memory_js_heap_mb: jsHeapMB,
        memory_dom_nodes: domNodes,
        memory_reload_reason: triggerReason,
      });
    } catch (e) {
      await recordServiceWorkerRecovery(base, tab, platform, "memory_reload_failed", triggerReason, {
        ...baseExtras,
        reload_error: (e && e.message) || String(e),
      });
    }
  }
  await chrome.storage.local.set({ [MEMORY_LAST_RELOAD_KEY]: lastByTab });
  const prevStatus = await getStatus();
  const prevCount = Number(prevStatus.memoryReloadCount || 0);
  const patch = { lastMemoryCheckAt: now, lastMemoryCheckReason: reason };
  if (reloaded > 0) {
    patch.memoryReloadCount = prevCount + reloaded;
    patch.lastMemoryReloadAt = now;
    patch.lastMemoryReloadReasons = reasons;
  }
  await setStatus(patch);
  return { checked, reloaded, reasons };
}

async function scheduleAlarm() {
  chrome.alarms.create(ALARM, { periodInMinutes: WATCHDOG_MIN });
  chrome.alarms.create(ALARM_REFRESH, { periodInMinutes: REFRESH_MIN });
  chrome.alarms.create(ALARM_MEMORY, { periodInMinutes: MEMORY_CHECK_INTERVAL_MIN });
  await setStatus({ swStartedAt: Date.now() });
  const ver = (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || "?";
  await log("info", `worker started v${ver} - jittered tabs + ${WATCHDOG_MIN}-min watchdog + ${REFRESH_MIN}-min refresh + ${MEMORY_CHECK_INTERVAL_MIN}-min memory check`);
  await reportBridgeHeartbeat("schedule_alarm");
  await reportScraperTabHeartbeats("schedule_alarm");
}

function runStartupRecovery(reason, options = {}) {
  const attempt = Number(options.attempt || 0);
  _startupRecoveryChain = _startupRecoveryChain
    .catch(() => null)
    .then(() => performStartupRecovery(reason, { ...options, attempt }))
    .catch((e) => log("warn", `${reason} recovery failed: ${e && e.message ? e.message : e}`));
  return _startupRecoveryChain;
}

async function performStartupRecovery(reason, options = {}) {
  const attempt = Number(options.attempt || 0);
  const retries = Math.max(0, Number(options.retries || 0));
  const force = !!options.force;
  const refreshTabs = !!options.refreshTabs;
  const openTabs = options.openTabs !== false;
  const refreshForce = options.refreshForce !== false;
  await setStatus({
    swStartedAt: Date.now(),
    lastStartupRecoveryAt: Date.now(),
    lastStartupRecoveryReason: reason,
    lastStartupRecoveryAttempt: attempt,
  });
  await log("info", `startup recovery ${reason}${attempt ? " retry " + attempt : ""}`);
  if (attempt === 0 && shouldResetPageRecoveryForStartup(reason)) {
    await clearPageRecoveryState(reason).catch(() => {});
  }
  await scheduleAlarm().catch((e) => log("warn", `${reason} schedule failed: ${e && e.message ? e.message : e}`));
  await syncCookies().catch(() => {});
  if (openTabs) {
    await ensureScraperTabsOpen(reason, { force })
      .catch((e) => log("warn", `${reason} tab audit failed: ${e && e.message ? e.message : e}`));
  }
  await reportBridgeHeartbeat(reason)
    .catch((e) => log("warn", `${reason} bridge heartbeat failed: ${e && e.message ? e.message : e}`));
  await reportScraperTabHeartbeats(reason)
    .catch((e) => log("warn", `${reason} scraper heartbeat failed: ${e && e.message ? e.message : e}`));
  if (refreshTabs) {
    const refreshGapMs = Number.isFinite(Number(options.refreshGapMs)) ? Number(options.refreshGapMs) : 1500;
    await refreshScraperTabs({ bypassCache: true, force: refreshForce || force, reason, gapMs: refreshGapMs })
      .catch((e) => log("warn", `${reason} tab refresh failed: ${e && e.message ? e.message : e}`));
    await reportScraperTabHeartbeats(reason + "_refresh")
      .catch((e) => log("warn", `${reason} refresh heartbeat failed: ${e && e.message ? e.message : e}`));
  }
  await ensureLoops(reason)
    .catch((e) => log("warn", `${reason} loop nudge failed: ${e && e.message ? e.message : e}`));

  if (attempt < retries) {
    const delay = [45000, 150000, 360000][Math.min(attempt, 2)];
    setTimeout(() => {
      runStartupRecovery(reason, { ...options, attempt: attempt + 1, retries });
    }, delay);
  }
}
// onInstalled fires on every extension reload/update — the exact moment content
// scripts in already-open tabs get SEVERED ("Extension context invalidated") and go
// silent. Messaging them (ensureLoops) can't revive a severed script, so we RELOAD
// the scraper tabs to respawn fresh content scripts immediately, instead of leaving
// them dead until the 75-min auto-refresh. This is the "I reloaded the extension and
// scraping stopped" fix.
chrome.runtime.onInstalled.addListener(() => {
  runStartupRecovery("installed", { force: true, refreshTabs: true, refreshGapMs: 1500, retries: 2 });
});
chrome.runtime.onStartup.addListener(() => {
  runStartupRecovery("startup", { force: false, refreshTabs: false, retries: 2 });
});

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name && a.name.startsWith(PAGE_RECOVERY_PREFIX)) { await runPageRecovery(a.name.slice(PAGE_RECOVERY_PREFIX.length)); }
  else if (a.name === EXTENSION_AUTO_RELOAD_ALARM) {
    try { chrome.runtime.reload(); } catch (e) {}
  }
  else if (a.name === ALARM) {
    await _alarmJitter();
    await ensureScraperTabsOpen("watchdog");
    await reportScraperTabHeartbeats("watchdog");
    await ensureLoops("watchdog");
  }
  else if (a.name === ALARM_REFRESH) {
    await _alarmJitter();
    await refreshScraperTabs({ reason: "scheduled" });
    await reportScraperTabHeartbeats("refresh");
    await syncCookies();
  }
  else if (a.name === ALARM_MEMORY) {
    await _alarmJitter();
    await checkMemorySensitiveTabs("scheduled");
  }
});

// scraper hosts that have a content-script scraper
function scraperPlatforms() { return (globalThis.UC_PLATFORMS || []).filter((p) => p.scraper); }
function platformHosts(p) {
  return [p && p.host, ...((p && p.aliasHosts) || [])].filter(Boolean);
}
function platformUrlPatterns(p) {
  return platformHosts(p).map((host) => `https://${host}/*`);
}
function scraperUrlPatterns() { return scraperPlatforms().flatMap(platformUrlPatterns); }
function platformById(id) { return scraperPlatforms().find((p) => p.id === id) || null; }
function pageRecoveryAlarm(tabId) { return PAGE_RECOVERY_PREFIX + String(tabId); }
function normalizeRecoveryUrl(url) {
  try {
    const u = new URL(url || "");
    return u.origin + u.pathname.replace(/\/$/, "");
  } catch (e) {
    return String(url || "").split("#")[0].split("?")[0];
  }
}
async function pageRecoveryState() {
  const { [PAGE_RECOVERY_STATE_KEY]: cur = {} } = await chrome.storage.local.get(PAGE_RECOVERY_STATE_KEY);
  const state = cur && typeof cur === "object" ? cur : {};
  const now = Date.now();
  let changed = false;
  for (const [key, rec] of Object.entries(state)) {
    const lastSeenAt = Number(rec && rec.lastSeenAt || 0);
    if (!lastSeenAt || now - lastSeenAt > PAGE_RECOVERY_STATE_TTL_MS) {
      delete state[key];
      changed = true;
    }
  }
  if (changed) await savePageRecoveryState(state);
  return state;
}
async function savePageRecoveryState(state) {
  await chrome.storage.local.set({ [PAGE_RECOVERY_STATE_KEY]: state || {} });
}
async function clearPageRecoveryState(reason) {
  try { await chrome.storage.local.remove(PAGE_RECOVERY_STATE_KEY); } catch (e) {}
  try {
    const alarms = await chrome.alarms.getAll();
    for (const alarm of alarms || []) {
      if (alarm && alarm.name && alarm.name.startsWith(PAGE_RECOVERY_PREFIX)) {
        await chrome.alarms.clear(alarm.name);
      }
    }
  } catch (e) {}
  await log("info", `page recovery state reset (${reason})`);
}
function shouldResetPageRecoveryForStartup(reason) {
  return /^(installed|manual_extension_reload|version_changed)$/i.test(String(reason || ""));
}
async function clearPageRecoveryForTab(tabId, reason) {
  if (tabId == null) return;
  const state = await pageRecoveryState();
  const prefix = String(tabId) + ":";
  let changed = false;
  for (const key of Object.keys(state)) {
    if (key.startsWith(prefix)) {
      delete state[key];
      changed = true;
    }
  }
  if (changed) {
    try { await chrome.alarms.clear(pageRecoveryAlarm(tabId)); } catch (e) {}
    await savePageRecoveryState(state);
    if (reason) await log("info", `page recovery cleared for tab ${tabId} (${reason})`);
  }
}
async function recordPageHealth(base, msg, sender, extra = {}) {
  try {
    const result = await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
      platform: msg.platform,
      label: msg.label || msg.platform,
      running: true,
      url: msg.url || (sender && sender.tab && sender.tab.url) || null,
      tab_id: sender && sender.tab ? sender.tab.id : null,
      health_status: msg.status || "unknown",
      health_reason: msg.reason || null,
      page_title: msg.title || null,
      text_sample: msg.sample || null,
      content_counts: msg.content_counts || null,
      cycle_reason: msg.cycle_reason || null,
      cycle_targets: msg.cycle_targets ?? null,
      cycle_saved: msg.cycle_saved ?? null,
      cycle_discovered: msg.cycle_discovered ?? null,
      cycle_error: msg.cycle_error || null,
      cooldown_left_ms: msg.cooldown_left_ms ?? null,
      loop_running: msg.loop_running ?? null,
      one_shot_running: msg.one_shot_running ?? null,
      one_shot_age_ms: msg.one_shot_age_ms ?? null,
      scrape_pass_running: msg.scrape_pass_running ?? null,
      scrape_pass_age_ms: msg.scrape_pass_age_ms ?? null,
      scrape_pass_reason: msg.scrape_pass_reason ?? null,
      stale_after_ms: msg.stale_after_ms ?? null,
      one_shot_timeout: msg.one_shot_timeout ?? null,
      timeout_ms: msg.timeout_ms ?? null,
      ...extra,
    }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
    const tab = sender && sender.tab ? sender.tab : null;
    const platform = platformById(msg.platform) || { id: msg.platform, label: msg.label || msg.platform };
    await scheduleMaybeForceScrapeCycle(tab, platform, result && result.body, msg.reason || msg.status || "page_health", base);
  } catch (e) {}
}
function recoveryDelayMs(attempt, platformId) {
  const windows = platformId === "x"
    ? [
        [8000, 20000],
        [45000, 90000],
        [120000, 240000],
      ]
    : PAGE_RECOVERY_DELAY_WINDOWS_MS;
  const idx = Math.min(windows.length - 1, Math.max(0, attempt - 1));
  const win = windows[idx];
  return _randBetween(win[0], win[1]);
}
function recoveryLimitCooldownMs(platformId) {
  return PAGE_RECOVERY_LIMIT_COOLDOWN_MS_BY_PLATFORM[platformId] || PAGE_RECOVERY_LIMIT_COOLDOWN_MS;
}
async function schedulePageRecovery(base, msg, sender) {
  const tab = sender && sender.tab;
  if (!tab || tab.id == null) return { ok: false, reason: "no_sender_tab" };
  const platform = platformById(msg.platform);
  if (!platform) return { ok: false, reason: "unknown_platform" };
  const url = msg.url || tab.url || "";
  const current = normalizeRecoveryUrl(url);
  const state = await pageRecoveryState();
  const key = `${tab.id}:${platform.id}`;
  const prev = state[key] || {};
  const samePage = prev.url === current;
  let attempts = samePage ? Number(prev.attempts || 0) : 0;
  const now = Date.now();
  const limitUntil = samePage ? Number(prev.limitUntil || 0) : 0;

  if (limitUntil && now < limitUntil) {
    const delay = Math.max(0, limitUntil - now);
    await recordPageHealth(base, msg, sender, {
      recovery_scheduled: false,
      recovery_attempt: attempts,
      recovery_limit: true,
      recovery_delay_ms: delay,
    });
    return {
      ok: true,
      scheduled: false,
      reason: "attempt_limit_cooling",
      attempt: attempts,
      cooldown_mins: Math.ceil(delay / 60000),
    };
  }
  if (limitUntil && now >= limitUntil) {
    attempts = 0;
  }

  if (prev.nextAt && now < prev.nextAt && samePage) {
    const delay = Math.max(0, prev.nextAt - now);
    await recordPageHealth(base, msg, sender, {
      recovery_scheduled: true,
      recovery_pending: true,
      recovery_attempt: attempts,
      recovery_delay_ms: delay,
    });
    return { ok: true, scheduled: true, pending: true, attempt: attempts, delay_ms: delay };
  }

  if (attempts >= PAGE_RECOVERY_MAX_ATTEMPTS) {
    const cooldownMs = recoveryLimitCooldownMs(platform.id);
    state[key] = { ...prev, url: current, lastSeenAt: now, limitLogged: true, limitUntil: now + cooldownMs };
    await savePageRecoveryState(state);
    if (!prev.limitLogged) {
      await log("warn", `${platform.label} page recovery hit attempt limit on tab ${tab.id}; cooling instead of reload loop`);
    }
    await recordPageHealth(base, msg, sender, {
      recovery_scheduled: false,
      recovery_attempt: attempts,
      recovery_limit: true,
      recovery_delay_ms: cooldownMs,
    });
    return { ok: true, scheduled: false, reason: "attempt_limit", attempt: attempts, cooldown_mins: Math.ceil(cooldownMs / 60000) };
  }

  attempts += 1;
  const delay = recoveryDelayMs(attempts, platform.id);
  state[key] = {
    url: current,
    platform: platform.id,
    attempt: attempts,
    attempts,
    nextAt: now + delay,
    lastSeenAt: now,
    reason: msg.reason || "recoverable_error_shell",
    title: msg.title || "",
  };
  await savePageRecoveryState(state);
  await chrome.alarms.create(pageRecoveryAlarm(tab.id), { when: now + delay });
  await log("warn", `${platform.label} page looks stuck (${msg.reason || "error shell"}); reload scheduled in ${Math.round(delay / 1000)}s (attempt ${attempts}/${PAGE_RECOVERY_MAX_ATTEMPTS})`);
  await recordPageHealth(base, msg, sender, {
    recovery_scheduled: true,
    recovery_attempt: attempts,
    recovery_delay_ms: delay,
  });
  return { ok: true, scheduled: true, attempt: attempts, delay_ms: delay };
}
async function runPageRecovery(tabId) {
  const state = await pageRecoveryState();
  const prefix = String(tabId) + ":";
  const key = Object.keys(state).find((k) => k.startsWith(prefix));
  if (!key) return;
  const rec = state[key] || {};
  try {
    const tab = await chrome.tabs.get(Number(tabId));
    const current = normalizeRecoveryUrl(tab && tab.url);
    if (!tab || current !== rec.url) {
      delete state[key];
      await savePageRecoveryState(state);
      await log("info", `page recovery skipped for tab ${tabId}; tab moved away`);
      return;
    }
    // If this platform gets home-URL hard-refresh on forced-cycle recovery, use
    // the same navigation strategy here: reloading a lemon8 SPA that's showing
    // "Not found" on /feed/food?region=sg just re-serves the same broken page.
    // Navigating to platform.url gets the tab back onto a fresh feed root.
    const platform = platformById(rec.platform);
    const navTarget = platform ? hardRefreshNavigationUrl(platform, tab.url, Date.now()) : null;
    if (navTarget) {
      await chrome.tabs.update(Number(tabId), { url: navTarget });
      state[key] = { ...rec, nextAt: 0, lastReloadAt: Date.now(), lastNavTo: navTarget };
      await savePageRecoveryState(state);
      await log("warn", `page recovery navigated tab ${tabId} to ${navTarget} (${rec.platform || "scraper"}, attempt ${rec.attempts || rec.attempt || 1})`);
      return;
    }
    await chrome.tabs.reload(Number(tabId), { bypassCache: true });
    state[key] = { ...rec, nextAt: 0, lastReloadAt: Date.now() };
    await savePageRecoveryState(state);
    await log("warn", `page recovery reloaded tab ${tabId} (${rec.platform || "scraper"}, attempt ${rec.attempts || rec.attempt || 1})`);
  } catch (e) {
    delete state[key];
    await savePageRecoveryState(state);
  }
}

// Nudge every open scraper tab to ensure its continuous loop is running. The tab
// auto-starts the loop on load; this only RESPAWNS it if it died (page reload,
// crash) or the service worker had been asleep. No scrape cadence here — pacing
// lives inside the loop (rate-limited + jittered).
async function ensureLoops(reason) {
  const forceCycle = /browser_content_stale|manual|scrape|tabs_page|stale/i.test(reason || "");
  const allTabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
  const selection = selectCanonicalScraperTabRows(allTabs);
  const tabs = selection.rows.map((row) => row.tab);
  if (!selection.seen) {
    await log("warn", `no scraper tab open — paused (${reason}). Open one via 🗂 Manage social tabs.`);
    await setStatus({ loopRunning: false });
    return false;
  }
  for (const t of tabs) {
    try {
      const host = new URL(t.url || "").hostname;
      if (_mainWorldHookHostRe.test(host) && chrome.scripting && chrome.scripting.executeScript) {
        await chrome.scripting.executeScript({
          target: { tabId: t.id, allFrames: false },
          files: ["inject.js"],
          world: "MAIN",
        });
      }
    } catch (e) {
      await log("warn", `page hook inject failed for tab ${t.id}: ${e && e.message ? e.message : e}`);
    }
    try {
      await sendTabMessageWithTimeout(t.id, { type: forceCycle ? "scrapeCycle" : "ensureLoop", reason });
    } catch (firstErr) {
      try {
        const platform = platformForTabUrl(t.url);
        const receiverMissing = isNoReceiverError(firstErr);
        const messageTimedOut = isTabMessageTimeout(firstErr);
        const cycleError = firstErr && firstErr.message ? firstErr.message : String(firstErr);
        if (platform && messageTimedOut) {
          const base = await ingestBase();
          const injected = await injectContentScriptAndNudge(base, t, platform, "ensure_loop_message_timeout", {
            cycle_error: cycleError,
            message_timeout: true,
            recovery: "message_timeout_programmatic_inject",
          });
          await recordServiceWorkerRecovery(base, t, platform, "content_script_message_timeout", reason || "ensure_loop", {
            cycle_error: cycleError,
            message_timeout: true,
            reinject_attempted: true,
            reinject_ok: injected || null,
          });
          await log("warn", `${platform.label}: scraper tab did not answer loop nudge within timeout; ${injected ? "re-injected content script and left it open" : "leaving it open to finish current work"} (${reason})`);
        } else if (platform && receiverMissing) {
          const base = await ingestBase();
          await recordServiceWorkerRecovery(base, t, platform, "content_script_missing_refresh", reason || "ensure_loop", {
            cycle_error: cycleError,
            no_receiver: true,
          });
          const reloaded = await refreshTabForMissingContentScript(base, t, platform, "ensure_loop_receiver_missing", {
            cycle_error: cycleError,
            no_receiver: true,
          });
          if (reloaded) {
            await log("warn", `content scraper missing in tab ${t.id}; hard-refreshed so manifest content.js can reload (${reason})`);
          } else {
            await log("warn", `content scraper missing in tab ${t.id}; hard-refresh already debounced (${reason})`);
          }
        } else {
          throw firstErr;
        }
      } catch (e) {
        await log("warn", `content scraper recovery failed for tab ${t.id}: ${e && e.message ? e.message : e}`);
      }
    }
  }
  return true;
}

const lastKick = {};
async function kick(reason, tabId) {
  if (tabId != null) {
    const now = Date.now();
    if (lastKick[tabId] && now - lastKick[tabId] < KICK_DEBOUNCE_MS) return;
    lastKick[tabId] = now;
  }
  return ensureLoops(reason);
}

// a scraper tab finished loading -> make sure its loop is running
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status !== "complete" || !tab.url) return;
  const host = (() => { try { return new URL(tab.url).host; } catch (e) { return ""; } })();
  if (scraperPlatforms().some((p) => platformHosts(p).includes(host))) kick("tab-loaded", tabId);
});

// ---- social tab launcher -------------------------------------------------
async function tabForPlatform(p) {
  const tabs = await chrome.tabs.query({ url: platformUrlPatterns(p) });
  return tabs && tabs[0] ? tabs[0] : null;
}
async function isLoggedIn(p) {
  try { const c = await chrome.cookies.get({ url: p.cookieUrl, name: p.cookie }); return !!(c && c.value); }
  catch (e) { return null; }
}
async function platformStatuses() {
  const out = [];
  for (const p of globalThis.UC_PLATFORMS || []) {
    const tab = await tabForPlatform(p);
    out.push({ id: p.id, label: p.label, url: p.url, host: p.host, scraper: !!p.scraper, noLogin: !!p.noLogin, tabOpen: !!tab, tabId: tab ? tab.id : null, loggedIn: await isLoggedIn(p) });
  }
  return out;
}

function browserUploadAllowed(platform, rawUrl) {
  let host = "";
  try { host = new URL(rawUrl || "").hostname.toLowerCase(); } catch (e) { return false; }
  const rules = {
    tiktok: /(^|\.)((tiktokcdn|tiktokv|byteoversea|byteimg|ibytedtos|muscdn)\.com)$/i,
    facebook: /(^|\.)(fbcdn\.net)$/i,
    threads: /(^|\.)((fbcdn\.net)|(cdninstagram\.com))$/i,
    instagram: /(^|\.)((fbcdn\.net)|(cdninstagram\.com))$/i,
    x: /(^|\.)((twimg\.com)|(twitter\.com)|(x\.com))$/i,
    lemon8: /(^|\.)((lemon8-app\.com)|(byteimg\.com)|(ibytedtos\.com))$/i,
  };
  return Boolean((rules[platform] || /$a/).test(host));
}

function shouldBrowserUploadMedia(item) {
  if (!item || !item.url) return false;
  if (item.browser_upload === true || item.browser_upload_only === true) return true;
  return String(item.content_type || "").toLowerCase() === "video";
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(out);
}

function platformUploadKey(platform) {
  return String(platform || "unknown").toLowerCase() || "unknown";
}

function browserUploadAttemptLimit(platform) {
  const n = Number(BROWSER_UPLOAD_ATTEMPT_LIMIT_BY_PLATFORM[platformUploadKey(platform)]);
  return Number.isFinite(n) && n > 0 ? n : 2;
}

async function fetchWithAbort(url, options, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...(options || {}), signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function uploadMediaViaBrowser(base, msg, item) {
  const key = platformUploadKey(msg.platform || "instagram");
  const prev = browserUploadChains[key] || Promise.resolve();
  const next = prev.catch(() => null).then(() => uploadMediaViaBrowserNow(base, msg, item));
  browserUploadChains[key] = next.catch(() => null);
  return next;
}

function safeUploadFilename(item, blob) {
  const rawKind = String(item && item.content_type ? item.content_type : "media").toLowerCase();
  const rawId = String(item && item.content_id ? item.content_id : "browser_upload");
  const kind = rawKind.replace(/[^a-z0-9_.-]+/g, "_").slice(0, 24) || "media";
  const id = rawId.replace(/[^a-zA-Z0-9_.-]+/g, "_").slice(0, 96) || "browser_upload";
  const mime = String(blob && blob.type ? blob.type : "").toLowerCase();
  const ext = mime.includes("png") ? ".png"
    : mime.includes("webp") ? ".webp"
    : mime.includes("gif") ? ".gif"
    : mime.includes("mp4") ? ".mp4"
    : mime.includes("jpeg") || mime.includes("jpg") ? ".jpg"
    : "";
  return `${kind}_${id}${ext}`;
}

async function postBrowserUpload(base, payload, blob) {
  const form = new FormData();
  form.append("metadata", JSON.stringify(payload));
  form.append("file", blob, safeUploadFilename(payload.item || {}, blob));
  let r = await fetchWithAbort(base + "/social/ingest-upload-binary", {
    method: "POST",
    body: form,
  }, BROWSER_UPLOAD_POST_TIMEOUT_MS);
  if (r.status !== 404 && r.status !== 405) return r;

  const b64 = arrayBufferToBase64(await blob.arrayBuffer());
  return fetchWithAbort(base + "/social/ingest-upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...payload,
      item: {
        ...(payload.item || {}),
        data_b64: b64,
      },
    }),
  }, BROWSER_UPLOAD_POST_TIMEOUT_MS);
}

async function uploadMediaViaBrowserNow(base, msg, item) {
  const platform = msg.platform || "instagram";
  if (!browserUploadAllowed(platform, item.url)) return { ok: false, reason: "disallowed_host" };
  try {
    const response = await fetchWithAbort(item.url, {
      credentials: "include",
      cache: "force-cache",
    }, BROWSER_UPLOAD_FETCH_TIMEOUT_MS);
    if (!response.ok) return { ok: false, reason: "http_" + response.status };
    const headerSize = Number(response.headers.get("content-length") || 0);
    if (headerSize && headerSize > BROWSER_UPLOAD_MAX_BYTES) return { ok: false, reason: "too_large_header", bytes: headerSize };
    const blob = await response.blob();
    if (blob.size > BROWSER_UPLOAD_MAX_BYTES) return { ok: false, reason: "too_large", bytes: blob.size };
    const payload = withExtensionVersion({
      platform,
      username: msg.username,
      item: {
        ...item,
        mime_type: blob.type || response.headers.get("content-type") || null,
        meta: { ...(item.meta || {}), browser_upload: true, browser_upload_transport: "multipart" },
      },
      file_size: blob.size,
      mime_type: blob.type || response.headers.get("content-type") || null,
    });
    const r = await postBrowserUpload(base, payload, blob);
    const j = await r.json().catch(() => ({}));
    const accepted = Number(j.accepted ?? j.stored ?? 0);
    const saved = Number(j.saved ?? (j.deduped ? 0 : j.stored || 0));
    const deduped = j.deduped === true ? 1 : 0;
    const rejectStats = j.reject_stats || null;
    const reason = j.reason || topRejectReason(rejectStats) || (r.ok ? "not_accepted" : "http_" + r.status);
    return {
      ok: r.ok && accepted > 0,
      accepted,
      stored: accepted,
      saved,
      deduped,
      reason,
      reject_stats: rejectStats,
    };
  } catch (e) {
    return { ok: false, reason: e && e.name === "AbortError" ? "timeout" : String(e.message || e) };
  }
}

function topRejectReason(stats) {
  if (!stats || typeof stats !== "object") return "";
  let best = "";
  let bestCount = -1;
  for (const [key, raw] of Object.entries(stats)) {
    const count = Number(raw || 0);
    if (count > bestCount) {
      best = key;
      bestCount = count;
    }
  }
  return best;
}

async function uploadBrowserMediaCandidates(base, msg, items) {
  const candidates = (items || []).filter(shouldBrowserUploadMedia);
  const limit = browserUploadAttemptLimit(msg.platform || "instagram");
  const activeCandidates = candidates.slice(0, limit);
  const deferredCandidates = candidates.slice(limit);
  let accepted = 0;
  let saved = 0;
  let deduped = 0;
  let attempted = 0;
  const failures = {};
  const failedCandidates = [];
  for (const item of activeCandidates) {
    attempted++;
    const result = await uploadMediaViaBrowser(base, msg, item);
    if (result.ok) {
      accepted += Number(result.accepted || 1);
      saved += Number(result.saved || 0);
      deduped += Number(result.deduped || 0);
    }
    else {
      failures[result.reason || "failed"] = (failures[result.reason || "failed"] || 0) + 1;
      failedCandidates.push({ item, result, ingest_mode: "browser_upload" });
    }
  }
  for (const item of deferredCandidates) {
    const result = {
      ok: false,
      reason: "deferred_upload_budget",
      upload_attempt_limit: limit,
    };
    failures.deferred_upload_budget = (failures.deferred_upload_budget || 0) + 1;
    failedCandidates.push({ item, result, ingest_mode: "browser_upload" });
  }
  if (attempted || deferredCandidates.length) {
    const detail = ` (${saved} new${deduped ? `, ${deduped} duplicate` : ""})`;
    await log(
      accepted ? (Object.keys(failures).length ? "warn" : "info") : "warn",
      `📦 ${msg.platform || "instagram"} · ${msg.username} · browser-upload ${accepted}/${attempted} accepted${detail}${deferredCandidates.length ? `, ${deferredCandidates.length} deferred` : ""}`
    );
    if (Object.keys(failures).length) await log("warn", `browser-upload misses: ${JSON.stringify(failures)}`);
  }
  if (failedCandidates.length) await recordBrowserMediaCandidateResults(base, msg, failedCandidates);
  return { attempted, deferred: deferredCandidates.length, stored: accepted, accepted, saved, deduped, failures };
}

async function recordBrowserMediaCandidateResults(base, msg, items) {
  try {
    const r = await fetch(base + "/social/browser-media-candidates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(withExtensionVersion({
        platform: msg.platform || "instagram",
        username: msg.username || "unknown",
        items,
      })),
    });
    if (!r.ok) await log("warn", `browser media candidate ledger failed: HTTP ${r.status}`);
  } catch (e) {
    await log("warn", `browser media candidate ledger failed: ${e.message || e}`);
  }
}
// Returns {opened|focused, tabId}. `active` brings the tab to the foreground so
// the user actually SEES it (the old version opened pinned+inactive, which made
// "Open all" look like nothing happened). New tabs join the social group when
// the user has grouped their scraper tabs (see scraperGroupHint).
async function openOrFocus(p, { active = false } = {}) {
  try {
    const tab = await tabForPlatform(p);
    if (tab) { await chrome.tabs.update(tab.id, { active }); return { id: p.id, focused: true, tabId: tab.id }; }
    const created = await createTabInSocialGroup({ url: p.url, active });
    await log("info", `opened tab: ${p.label}`);
    return { id: p.id, opened: true, tabId: created.id };
  } catch (e) {
    await log("error", `open ${p.label} failed: ${e.message}`);
    return { id: p.id, error: String(e.message || e) };
  }
}

// ---- message router ------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Fast-path for high-frequency fire-and-forget message types: reply
  // synchronously BEFORE any await, so a cold-start of the service worker
  // (which pays for a chrome.storage.local.get in ingestBase()) can't push
  // us past the content-script's 10-15 s deadline. All these callers use
  // .catch(()=>{}) and never consume the return value, and log() /
  // setStatus() / postJsonWithTimeout() etc. keep the SW alive on their own.
  const fastReplyTypes = new Set(["log", "tabReady", "loopStatus", "cycleReport"]);
  if (msg && fastReplyTypes.has(msg.type)) {
    sendResponse({ ok: true });
    // Continue heavy work asynchronously; the outer listener still returns
    // true below so Chrome doesn't complain about a closed channel.
    (async () => {
      try {
        const base = await ingestBase();
        if (msg.type === "log") {
          await log(msg.level || "info", `[${msg.platform || "?"}] ${msg.msg}`);
          return;
        }
        if (msg.type === "tabReady") {
          return; // already acked; nothing else to do
        }
        if (msg.type === "loopStatus") {
          await setStatus({
            loopRunning: !!msg.running,
            loopPlatform: msg.platform,
            loopPlatformLabel: msg.label || msg.platform,
            lastLoopPing: Date.now(),
          });
          try {
            const tab = sender && sender.tab ? sender.tab : null;
            const platform = (globalThis.UC_PLATFORMS || []).find((p) => p.id === msg.platform) || {
              id: msg.platform,
              label: msg.label || msg.platform,
            };
            const result = await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
              platform: msg.platform,
              label: msg.label || null,
              running: !!msg.running,
              url: msg.url || (tab && tab.url) || null,
              tab_id: tab ? tab.id : null,
              health_status: msg.health_status || null,
              health_reason: msg.health_reason || null,
              message_type: msg.type || null,
              loop_running: !!msg.running,
              content_age_seconds: msg.content_age_seconds ?? null,
              stale_after_ms: msg.stale_after_ms ?? null,
            }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
            await maybeForceScrapeCycle(tab, platform, result && result.body, "loop_status", base);
          } catch (e) { /* swallow: content already got its ack */ }
          return;
        }
        if (msg.type === "cycleReport") {
          await setStatus({ lastCycleAt: Date.now(), lastCycle: { platform: msg.platform, targets: msg.targets, saved: msg.saved, discovered: msg.discovered } });
          await clearPageRecoveryForTab(sender && sender.tab ? sender.tab.id : null, "cycle-ok");
          await log("info", `✅ cycle done [${msg.platform}]: ${msg.targets} targets, ${msg.saved} media candidate(s), ${msg.discovered} discovered`);
          return;
        }
      } catch (e) {
        try { await log("error", `fast-path ${msg.type} failed: ${e && e.message ? e.message : e}`); } catch (_) {}
      }
    })();
    return true;
  }
  (async () => {
    try {
      const base = await ingestBase();
      switch (msg.type) {
      case "igCooldown": {
        // Ask the bridge whether the HEADLESS collector is in a 429 cooldown so the
        // extension can rest in sync (shared IG account → cooperative anti-ban).
        try {
          const account = String(msg.account || msg.owner || "").trim();
          const qs = account ? `?account=${encodeURIComponent(account)}` : "";
          const r = await fetch(base + "/social/ig_cooldown" + qs);
          sendResponse(await r.json());
        } catch (e) {
          sendResponse({ cooling: false, secs_left: 0, streak: 0 });
        }
        break;
      }
      case "getTargets": {
        try {
          const r = await fetch(base + `/social/targets?platform=${encodeURIComponent(msg.platform || "instagram")}`);
          const j = await r.json();
          await setStatus({ ingestOk: true, lastIngestCheck: Date.now() });
          sendResponse(j.targets || []);
        } catch (e) {
          await setStatus({ ingestOk: false, lastIngestCheck: Date.now() });
          await log("error", `getTargets failed: ${e.message} (is ig_ingest up on ${base}?)`);
          sendResponse([]);
        }
        break;
      }
      case "getXProfileTarget": {
        try {
          const owner = String(msg.owner || "").trim();
          const qs = owner ? `?owner=${encodeURIComponent(owner)}` : "";
          const r = await fetch(base + "/social/x-profile-target" + qs);
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, target: null, error: String(e.message || e) });
        }
        break;
      }
      case "xProfileTargetResult": {
        try {
          const r = await fetch(base + "/social/x-profile-target-result", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              username: msg.username,
              status: msg.status || "success",
              reason: msg.reason || null,
              owner: msg.owner || null,
            })),
          });
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "getTikTokRevisitTarget": {
        try {
          const owner = String(msg.owner || "").trim();
          const qs = owner ? `?owner=${encodeURIComponent(owner)}` : "";
          const r = await fetch(base + "/social/tiktok-revisit-target" + qs);
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, target: null, error: String(e.message || e) });
        }
        break;
      }
      case "getBrowserMediaRevisitTarget": {
        try {
          const platform = String(msg.platform || "").trim();
          const owner = String(msg.owner || "").trim();
          const params = new URLSearchParams();
          if (platform) params.set("platform", platform);
          if (owner) params.set("owner", owner);
          const qs = params.toString() ? "?" + params.toString() : "";
          const r = await fetch(base + "/social/browser-revisit-target" + qs);
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, target: null, error: String(e.message || e) });
        }
        break;
      }
      case "browserMediaRevisitResult": {
        try {
          const r = await fetch(base + "/social/browser-revisit-result", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform,
              content_id: msg.content_id,
              status: msg.status || "success",
              reason: msg.reason || null,
              observed: msg.observed ?? null,
              stored: msg.stored ?? null,
              username: msg.username || null,
            })),
          });
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "tiktokRevisitResult": {
        try {
          const r = await fetch(base + "/social/tiktok-revisit-result", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              content_id: msg.content_id,
              status: msg.status || "success",
              reason: msg.reason || null,
              observed: msg.observed ?? null,
              stored: msg.stored ?? null,
              username: msg.username || null,
            })),
          });
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "ingest": {
        try {
          const allItems = Array.isArray(msg.items) ? msg.items : [];
          const urlItems = allItems.filter((it) => !(it && it.browser_upload_only === true));
          const r = await fetch(base + "/social/ingest", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform || "instagram",
              username: msg.username,
              items: urlItems,
              record_empty: !!msg.record_empty,
              probe_reason: msg.probe_reason || null,
              probe_meta: msg.probe_meta || null,
            })),
          });
          const j = await r.json().catch(() => ({}));
          const upload = await uploadBrowserMediaCandidates(base, msg, allItems);
          await log("info", `📥 ${msg.platform || "instagram"} · ${msg.username} · ${j.accepted ?? urlItems.length} URL media queued, ${upload.stored}/${upload.attempted} browser-uploaded`);
          sendResponse({ ok: r.ok, upload });
        } catch (e) {
          await log("error", `ingest ${msg.username} failed: ${e.message}`);
          sendResponse({ ok: false, error: String(e) });
        }
        break;
      }
      case "discover": {
        try {
          const r = await fetch(base + "/social/discover", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform || "instagram", source: msg.source, hop: msg.hop, discovered: msg.discovered })),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `🕸 ${msg.platform || "instagram"} · spider from ${msg.source} (hop ${msg.hop}) · +${j.added ?? "?"} new accounts`);
          sendResponse({ ok: r.ok });
        } catch (e) {
          await log("error", `discover failed: ${e.message}`);
          sendResponse({ ok: false, error: String(e) });
        }
        break;
      }
      case "targetStatus": {
        try {
          const r = await fetch(base + "/social/target-status", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform || "instagram",
              username: msg.username,
              status: msg.status,
              reason: msg.reason || null,
            })),
          });
          sendResponse({ ok: r.ok });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "posts": {  // structured post metadata (captions/likes/comments counts)
        try {
          const r = await fetch(base + "/social/posts", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform || "instagram", username: msg.username, posts: msg.posts })),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `📝 ${msg.platform || "instagram"} · ${msg.username} · ${j.saved ?? msg.posts.length} post(s) w/ captions+counts`);
          sendResponse({ ok: r.ok });
        } catch (e) { await log("error", `posts failed: ${e.message}`); sendResponse({ ok: false }); }
        break;
      }
      case "comments": {  // comment threads
        try {
          const r = await fetch(base + "/social/comments", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform || "instagram", post_id: msg.post_id, comments: msg.comments })),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `💬 ${msg.platform || "instagram"} · post ${msg.post_id} · ${j.saved ?? "?"} comment(s)`);
          sendResponse({ ok: r.ok });
        } catch (e) { await log("error", `comments failed: ${e.message}`); sendResponse({ ok: false }); }
        break;
      }
      case "seed": {  // seed the spider from your own followers/following (hop 0)
        try {
          const r = await fetch(base + "/social/seed", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform || "instagram", users: msg.users })),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `self-seed: +${j.added ?? "?"} seed target(s)`);
          sendResponse({ ok: r.ok });
        } catch (e) { sendResponse({ ok: false }); }
        break;
      }
      case "profile": {  // full profile -> instagram_profiles + social_users + photo
        try {
          const r = await fetch(base + "/social/profile", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform || "instagram",
              profile: msg.profile,
              owner: msg.owner || null,
            })),
          });
          sendResponse({ ok: r.ok });
        } catch (e) { sendResponse({ ok: false }); }
        break;
      }
      case "users": {  // universal user registry — anyone we encountered
        try {
          const r = await fetch(base + "/social/users", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform || "instagram", context: msg.context || "seen", owner: msg.owner || null, users: msg.users })),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `👤 ${msg.platform || "instagram"} · +${j.recorded ?? "?"} users (via ${msg.context || "seen"})`);
          sendResponse({ ok: r.ok });
        } catch (e) { await log("error", `users failed: ${e.message}`); sendResponse({ ok: false }); }
        break;
      }
      case "dms": {  // Instagram DMs observed from the direct_v2 responses
        try {
          const r = await fetch(base + "/social/dms", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform || "instagram", owner: msg.owner || null, threads: msg.threads })),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `✉️ ${msg.platform || "instagram"} · +${j.recorded ?? "?"} DM messages`);
          sendResponse({ ok: r.ok });
        } catch (e) { await log("error", `dms failed: ${e.message}`); sendResponse({ ok: false }); }
        break;
      }
      case "tiktok_dm":       // DM JSON frame from a WS (rare) — capture (#35)
      case "instagram_dm": {
        try {
          const r = await fetch(base + "/social/dm-frame", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform, frame: msg.frame })),
          });
          sendResponse({ ok: r.ok });
        } catch (e) { sendResponse({ ok: false }); }
        break;
      }
      case "dm_probe": {  // one-time DM transport/format probe for investigation (#38)
        try {
          await fetch(base + "/social/dm-probe", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform, transport: msg.transport,
              url: msg.url, frame_kind: msg.frame_kind, frame_size: msg.frame_size })),
          });
          await log("info", `🔎 ${msg.platform} DM ${msg.transport}: ${msg.frame_kind || "url"} ${msg.frame_size ? "~" + msg.frame_size + "B " : ""}${msg.url || ""}`);
        } catch (e) {}
        sendResponse({ ok: true });
        break;
      }
      case "dm_sample": {  // raw DM-socket frame bytes for decoder work (#35)
        try {
          await fetch(base + "/social/dm-sample", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform, url: msg.url, size: msg.size, b64: msg.b64 })),
          });
          await log("info", `🧪 ${msg.platform} DM sample: ${msg.size}B`);
        } catch (e) {}
        sendResponse({ ok: true });
        break;
      }
      case "dm_heartbeat": {  // WS-hook liveness pulse for the freshness watchdog (P1.3)
        try {
          // Enrich with owner (ds_user_id for IG; TikTok has no equivalent
          // cookie name universally — leave blank on non-IG so the bridge
          // upserts to (platform, '')).
          let owner = "";
          try {
            if (msg.platform === "instagram") {
              const c = await chrome.cookies.get({ url: "https://www.instagram.com/", name: "ds_user_id" });
              owner = (c && c.value) || "";
            }
          } catch (e) { /* cookies perm not granted */ }
          await fetch(base + "/social/dm-heartbeat", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform, owner,
              probes_sent: msg.probes_sent || 0,
              samples_shipped: msg.samples_shipped || 0,
            })),
          });
        } catch (e) {}
        sendResponse({ ok: true });
        break;
      }
      case "dm_decoded": {  // Option B: client-decoded DM payload → structured upsert
        try {
          await fetch(base + "/social/dm-decoded", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform, owner: msg.owner || "",
              threads: msg.threads || [], messages: msg.messages || [],
            })),
          });
          const n = (msg.messages && msg.messages.length) || 0;
          if (n) await log("info", `📨 ${msg.platform} DM decoded: ${n} msg`);
        } catch (e) {}
        sendResponse({ ok: true });
        break;
      }
      case "getStravaRouteQueue": {
        try {
          const limit = Math.max(1, Math.min(Number(msg.limit || 1), 10));
          const owner = String(msg.owner || msg.account || "").trim();
          const accountQuery = owner ? `&account=${encodeURIComponent(owner)}` : "";
          const query = `?limit=${limit}${accountQuery}`;
          let result;
          try {
            const ctl = await controlBase();
            result = await fetchJsonWithTimeout(ctl + `/strava/route-capture-queue${query}`);
          } catch (e) {
            result = await fetchJsonWithTimeout(base + `/social/strava-route-queue${query}`, 30000);
          }
          sendResponse({ ok: result.response.ok, ...result.json });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e), items: [] });
        }
        break;
      }
      case "stravaRouteVisit": {
        try {
          const r = await fetch(base + "/social/strava-route-visit", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              activity_id: msg.activity_id,
              activity_url: msg.activity_url || null,
              url: msg.url || null,
              status: msg.status || "observed",
              owner: msg.owner || msg.account || null,
            })),
          });
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "strava_streams": {  // passive route stream observed from Strava's own browser request
        try {
          const pointCount = msg.point_count || (msg.streams && msg.streams.latlng && msg.streams.latlng.length) || 0;
          const r = await fetch(base + "/social/strava-streams", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withExtensionVersion({
              platform: "strava",
              activity_id: msg.activity_id,
              request_url: msg.request_url || msg.url || null,
              http_status: msg.http_status || null,
              owner: msg.owner || msg.account || null,
              streams: msg.streams || {},
              point_count: pointCount,
            })),
          });
          const j = await r.json().catch(() => ({}));
          if (j.stored) await log("info", `🗺 strava · activity ${msg.activity_id} · route ${j.point_count || pointCount} point(s) captured`);
          else if (j.rate_limit_recorded) await log("warn", `strava · activity ${msg.activity_id} · HTTP ${msg.http_status} route stream recorded`);
          else if (j.reason === "no_route_points") await log("warn", `strava · activity ${msg.activity_id} · no GPS points returned; pausing retries`);
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          await log("error", `strava route capture failed: ${e.message}`);
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "pageHealth": {
        if (msg.status === "recoverable_error_shell") {
          // Response carries recovery.cooldown_mins, which the content
          // script consumes to set a throttle wall. Keep this path awaited.
          sendResponse(await schedulePageRecovery(base, msg, sender));
        } else if (msg.status === "healthy") {
          // Fire-and-forget: content script uses .catch(()=>null) and never
          // reads the response — reply first so a slow ingest fetch cannot
          // push us over the 20 s content-side deadline.
          sendResponse({ ok: true });
          await clearPageRecoveryForTab(sender && sender.tab ? sender.tab.id : null, "healthy");
          await recordPageHealth(base, msg, sender, { recovery_scheduled: false });
        } else {
          sendResponse({ ok: true });
          await recordPageHealth(base, msg, sender, { recovery_scheduled: false });
        }
        break;
      }
      case "swFetchProxy": {
        // Extension-origin fetch proxy for content scripts running on page
        // origins subject to either:
        //   (a) Chrome's Local Network Access / PNA enforcement (observed on
        //       https://www.lemon8-app.com) — Chrome blocks the request BEFORE
        //       preflight with "Permission was denied for this request to
        //       access the `loopback` address space."
        //   (b) The page's own CSP `connect-src` (instagram/threads/facebook) —
        //       Meta's template allows ws://localhost:* but NOT http://127.0.0.1:*,
        //       so a direct fetch produces a "Refused to connect" console error.
        // The SW's own fetches are exempt from BOTH (extension origin +
        // host_permissions declares 127.0.0.1/*), so we do the network call
        // here and hand the JSON body back.
        try {
          const path = String(msg.path || "");
          const payload = msg.payload || {};
          if (!path.startsWith("/")) {
            sendResponse({ ok: false, error: "invalid path" });
            break;
          }
          let finalPayload = withExtensionVersion(payload);
          // For proxied heartbeats, overwrite `tab_id: "content_direct"` (the
          // placeholder set by content.js because it can't see its own tab.id)
          // with the real sender.tab.id so the ingest bridge records the tab
          // that actually sent the heartbeat.
          if (
            path === "/social/browser-heartbeat"
            && sender && sender.tab && Number.isFinite(sender.tab.id)
          ) {
            finalPayload = { ...finalPayload, tab_id: sender.tab.id };
          }
          const proxied = await postJsonWithTimeout(
            base + path,
            finalPayload,
            SCRAPER_HEARTBEAT_TIMEOUT_MS
          );
          let parsedBody = null;
          try { parsedBody = proxied && proxied.body ? JSON.parse(proxied.body) : null; } catch (_) {}
          sendResponse({
            ok: !!(proxied && proxied.response && proxied.response.ok),
            status: proxied && proxied.response ? proxied.response.status : 0,
            body: parsedBody,
          });
        } catch (e) {
          sendResponse({ ok: false, error: String((e && e.message) || e) });
        }
        break;
      }
      case "wall": {  // the in-tab loop hit a throttle/login wall and is sleeping
        const mins = msg.mins || 45;
        await setStatus({ cooldownUntil: Date.now() + mins * 60000 });
        const account = String(msg.account || msg.owner || "").trim();
        const who = account ? `/${account}` : "";
        const reason = msg.reason ? ` (${msg.reason})` : "";
        await log("warn", `⚠️ ${msg.platform || "?"}${who} throttle wall${reason} — loop sleeping ${mins}m`);
        sendResponse({ ok: true });
        break;
      }
      case "loopStatus":
      case "tabReady":
      case "cycleReport":
      case "log":
        // Handled by the fast-path above; this catch-all keeps the slow-path
        // switch tidy if a straggler reaches here.
        sendResponse({ ok: true });
        break;
      case "getPlatforms":
        sendResponse(await platformStatuses());
        break;
      case "diagnostics": {
        const stored = await chrome.storage.local.get(["ucStatus", LOG_KEY, "ingestBase"]);
        sendResponse({
          ok: true,
          id: chrome.runtime.id,
          version: extensionVersion(),
          ingestBase: stored.ingestBase || DEFAULT_INGEST,
          status: stored.ucStatus || {},
          log: (stored[LOG_KEY] || []).slice(-20),
        });
        break;
      }
      case "testIngest": {
        sendManualIngestProbe()
          .then((result) => {
            reportScraperTabHeartbeats("manual_ingest_probe")
              .catch((e) => log("warn", `manual scraper heartbeat fan-out failed: ${e && e.message ? e.message : e}`));
            return result;
          })
          .then(sendResponse);
        return;
      }
      case "openPlatform": {
        const p = (globalThis.UC_PLATFORMS || []).find((x) => x.id === msg.id);
        sendResponse(p ? await openOrFocus(p, { active: true }) : { error: "unknown platform" });
        break;
      }
      case "openAll": {
        let opened = 0, focused = 0, errors = 0, first = true;
        for (const p of globalThis.UC_PLATFORMS || []) {
          const r = await openOrFocus(p, { active: first }); // focus the first so it's visible
          first = false;
          if (r.opened) opened++; else if (r.focused) focused++; else errors++;
        }
        await log("info", `open-all: ${opened} opened, ${focused} already open${errors ? ", " + errors + " failed" : ""}`);
        sendResponse({ opened, focused, errors });
        break;
      }
      case "refreshScraperTabs": {
        const result = await refreshScraperTabs({
          bypassCache: msg.bypassCache !== false,
          force: true,
          reason: msg.reason || "manual_tabs_page",
        });
        await reportScraperTabHeartbeats("manual_refresh_tabs")
          .catch((e) => log("warn", `manual refresh heartbeat failed: ${e && e.message ? e.message : e}`));
        sendResponse(result);
        break;
      }
      case "scrapeNow":
        scrapeNow().then(sendResponse);
        return;
      default:
        sendResponse({ ok: false, error: "unknown message" });
      }
    } catch (e) {
      const err = String(e && e.message ? e.message : e);
      await log("error", `message ${msg && msg.type ? msg.type : "unknown"} failed: ${err}`);
      sendResponse({ ok: false, error: err });
    }
  })();
  return true; // async
});

async function scrapeNow() {
  await log("info", "manual 'Start/Resume loop' clicked");
  await setStatus({ cooldownUntil: 0 });
  return { ok: await ensureLoops("manual") };
}

async function consumeReloadIntent() {
  let intent = null;
  try {
    const found = await chrome.storage.local.get(RELOAD_INTENT_KEY);
    intent = found && found[RELOAD_INTENT_KEY];
  } catch (e) {}
  if (!intent || !intent.requested_at) return false;

  const ageMs = Date.now() - Number(intent.requested_at || 0);
  try { await chrome.storage.local.remove(RELOAD_INTENT_KEY); } catch (e) {}
  if (!Number.isFinite(ageMs) || ageMs < 0 || ageMs > 10 * 60 * 1000) {
    await log("warn", "ignored stale extension reload intent");
    return false;
  }

  await log("info", "extension reload intent consumed; hard-refreshing scraper tabs");
  setTimeout(() => {
    (async () => {
      const openIds = Array.isArray(intent.open_ids) ? intent.open_ids.slice(0, 20) : [];
      for (const id of openIds) {
        const p = (globalThis.UC_PLATFORMS || []).find((x) => x.id === id);
        if (p) {
          await openOrFocus(p, { active: false }).catch((e) => log("warn", `reload intent open ${id} failed: ${e && e.message ? e.message : e}`));
          await _sleep(900);
        }
      }
      await runStartupRecovery("manual_extension_reload", {
        force: intent && intent.force_open_all === true,
        openTabs: intent && intent.force_open_all === true,
        refreshTabs: intent && intent.force_refresh_tabs !== false,
        refreshForce: true,
        refreshGapMs: 1500,
        retries: 3,
      });
      if (intent && intent.force_scrape) {
        await ensureLoops("manual_extension_reload_scrape")
          .catch((e) => log("warn", `reload intent scrape nudge failed: ${e && e.message ? e.message : e}`));
      }
      if (intent && intent.force_test) {
        await sendManualIngestProbe()
          .catch((e) => log("warn", `reload intent ingest probe failed: ${e && e.message ? e.message : e}`));
        await reportScraperTabHeartbeats("manual_extension_reload_probe")
          .catch((e) => log("warn", `reload intent scraper heartbeat failed: ${e && e.message ? e.message : e}`));
      }
    })().catch((e) => log("warn", `reload-intent recovery failed: ${e && e.message ? e.message : e}`));
  }, 500);
  return true;
}

async function rememberExtensionVersion() {
  const version = extensionVersion() || "?";
  try {
    const found = await chrome.storage.local.get(LOADED_VERSION_KEY);
    const previous = found && found[LOADED_VERSION_KEY] ? String(found[LOADED_VERSION_KEY]) : null;
    if (previous !== version) {
      await chrome.storage.local.set({ [LOADED_VERSION_KEY]: version });
      return { changed: true, previous, version };
    }
    return { changed: false, previous, version };
  } catch (e) {
    return { changed: false, previous: null, version };
  }
}

// Warm start (worker waking from sleep or after chrome.runtime.reload()).
// Wrapped so a rejection here does not become an unhandledrejection that
// tears down the SW into "bad state". Any failure is captured via _reportSwCrash
// (see top of file) and left for the alarm-based recovery to retry.
(async () => {
  try {
    await setStatus({ swStartedAt: Date.now() });
    await log("info", "service worker active");
    const versionState = await rememberExtensionVersion();
    const consumed = await consumeReloadIntent()
      .catch((e) => {
        log("warn", `reload intent handling failed: ${e && e.message ? e.message : e}`);
        return false;
      });
    if (!consumed) {
      if (versionState.changed) {
        const from = versionState.previous || "unknown";
        await log("info", `extension version changed ${from} -> ${versionState.version}; hard-refreshing scraper tabs`);
        setTimeout(() => {
          runStartupRecovery("version_changed", { force: true, refreshTabs: true, refreshGapMs: 1500, retries: 3 })
            .catch((e) => log("warn", `version-change recovery failed: ${e && e.message ? e.message : e}`));
        }, 500);
      } else {
        await runStartupRecovery("warm_start", { force: false, refreshTabs: false, retries: 2 });
      }
    }
  } catch (e) {
    try { _reportSwCrash({ kind: "warm_start_throw", message: e && e.message, stack: e && e.stack }); } catch (_) {}
    try { await log("warn", `warm-start crashed: ${e && e.message ? e.message : e}`); } catch (_) {}
  }
})();
