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

importScripts("platforms.js"); // defines globalThis.UC_PLATFORMS

const ALARM = "uc-scrape";
const DEFAULT_INGEST = "http://127.0.0.1:8765";
const LOG_KEY = "ucLog";
const LOG_MAX = 200;
const WATCHDOG_MIN = 10;         // re-nudge any open scraper tab whose loop died
const KICK_DEBOUNCE_MS = 30000;  // don't re-nudge the same tab more often than this

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
async function ingestBase() {
  const { ingestBase } = await chrome.storage.local.get("ingestBase");
  return ingestBase || DEFAULT_INGEST;
}

// ---- watchdog + AUTO-TABS (open + refresh) --------------------------------
// The 83h-stall problem: a closed/orphaned tab = no scraping. So the worker now
// auto-OPENS every scraper tab (pinned, background) and auto-REFRESHES them
// hourly — reloading respawns the content script + loop AND pulls fresh content,
// so it can never silently die again.
const ALARM_REFRESH = "uc-refresh";
const REFRESH_MIN = 75;

async function autoTabsEnabled() {
  const { ucAutoTabs } = await chrome.storage.local.get("ucAutoTabs");
  return ucAutoTabs !== false; // default ON
}
const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let _tabsOpInProgress = false; // guard against overlapping open/refresh runs (no spam)

// Open exactly the missing scraper tabs — pinned, background, ONE at a time with a
// gap so we never spam tabs or spike CPU. Robust dedup by host+path-prefix means a
// tab is never duplicated.
async function ensureScraperTabsOpen(reason) {
  if (!(await autoTabsEnabled()) || _tabsOpInProgress) return;
  _tabsOpInProgress = true;
  let opened = 0;
  try {
    for (const p of scraperPlatforms()) {
      const existing = await chrome.tabs.query({ url: `*://${p.host}/*` });
      for (const u of [p.url, ...(p.extraUrls || [])]) {
        let path = "/";
        try { path = new URL(u).pathname.split("?")[0].replace(/\/$/, "") || "/"; } catch (e) {}
        const has = (existing || []).some((t) => {
          try { const tp = new URL(t.url).pathname.replace(/\/$/, "") || "/"; return tp === path || (path !== "/" && tp.startsWith(path)); }
          catch (e) { return false; }
        });
        if (!has) {
          try { const t = await chrome.tabs.create({ url: u, pinned: true, active: false }); existing.push(t); opened++; await _sleep(1500); } catch (e) {}
        }
      }
    }
    if (opened) await log("info", `auto-opened ${opened} scraper tab(s) (${reason})`);
  } finally { _tabsOpInProgress = false; }
}

// Reload scraper tabs ONE at a time with a gap (staggered) so the loop respawns
// fresh without reloading 7 tabs simultaneously (CPU spike / overload).
async function refreshScraperTabs() {
  if (!(await autoTabsEnabled()) || _tabsOpInProgress) return;
  _tabsOpInProgress = true;
  try {
    const tabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
    for (const t of tabs || []) { try { await chrome.tabs.reload(t.id, { bypassCache: false }); await _sleep(3000); } catch (e) {} }
    await log("info", `auto-refreshed ${tabs ? tabs.length : 0} scraper tab(s), staggered → loop respawns fresh`);
  } finally { _tabsOpInProgress = false; }
}

async function scheduleAlarm() {
  chrome.alarms.create(ALARM, { periodInMinutes: WATCHDOG_MIN });
  chrome.alarms.create(ALARM_REFRESH, { periodInMinutes: REFRESH_MIN });
  await setStatus({ swStartedAt: Date.now() });
  await log("info", `worker started; auto-tabs + ${WATCHDOG_MIN}-min watchdog + ${REFRESH_MIN}-min refresh`);
}
chrome.runtime.onInstalled.addListener(() => { scheduleAlarm(); ensureScraperTabsOpen("installed").then(() => ensureLoops("installed")); });
chrome.runtime.onStartup.addListener(() => { scheduleAlarm(); ensureScraperTabsOpen("startup").then(() => ensureLoops("startup")); });

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name === ALARM) { await ensureScraperTabsOpen("watchdog"); await ensureLoops("watchdog"); }
  else if (a.name === ALARM_REFRESH) { await refreshScraperTabs(); }
});

// scraper hosts that have a content-script scraper
function scraperPlatforms() { return (globalThis.UC_PLATFORMS || []).filter((p) => p.scraper); }
function scraperUrlPatterns() { return scraperPlatforms().map((p) => `https://${p.host}/*`); }

// Nudge every open scraper tab to ensure its continuous loop is running. The tab
// auto-starts the loop on load; this only RESPAWNS it if it died (page reload,
// crash) or the service worker had been asleep. No scrape cadence here — pacing
// lives inside the loop (rate-limited + jittered).
async function ensureLoops(reason) {
  const tabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
  if (!tabs || !tabs.length) {
    await log("warn", `no scraper tab open — paused (${reason}). Open one via 🗂 Manage social tabs.`);
    await setStatus({ loopRunning: false });
    return false;
  }
  for (const t of tabs) {
    try { await chrome.tabs.sendMessage(t.id, { type: "ensureLoop" }); } catch (e) {}
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
  if (scraperPlatforms().some((p) => host === p.host)) kick("tab-loaded", tabId);
});

// ---- social tab launcher -------------------------------------------------
async function tabForHost(host) {
  const tabs = await chrome.tabs.query({ url: `*://${host}/*` });
  return tabs && tabs[0] ? tabs[0] : null;
}
async function isLoggedIn(p) {
  try { const c = await chrome.cookies.get({ url: p.cookieUrl, name: p.cookie }); return !!(c && c.value); }
  catch (e) { return null; }
}
async function platformStatuses() {
  const out = [];
  for (const p of globalThis.UC_PLATFORMS || []) {
    const tab = await tabForHost(p.host);
    out.push({ id: p.id, label: p.label, url: p.url, host: p.host, scraper: !!p.scraper, noLogin: !!p.noLogin, tabOpen: !!tab, tabId: tab ? tab.id : null, loggedIn: await isLoggedIn(p) });
  }
  return out;
}
// Returns {opened|focused, tabId}. `active` brings the tab to the foreground so
// the user actually SEES it (the old version opened pinned+inactive, which made
// "Open all" look like nothing happened).
async function openOrFocus(p, { active = false } = {}) {
  try {
    const tab = await tabForHost(p.host);
    if (tab) { await chrome.tabs.update(tab.id, { active }); return { id: p.id, focused: true, tabId: tab.id }; }
    const created = await chrome.tabs.create({ url: p.url, active });
    await log("info", `opened tab: ${p.label}`);
    return { id: p.id, opened: true, tabId: created.id };
  } catch (e) {
    await log("error", `open ${p.label} failed: ${e.message}`);
    return { id: p.id, error: String(e.message || e) };
  }
}

// ---- message router ------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    const base = await ingestBase();
    switch (msg.type) {
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
      case "ingest": {
        try {
          const r = await fetch(base + "/social/ingest", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform: msg.platform || "instagram", username: msg.username, items: msg.items }),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `ingest[${msg.platform || "instagram"}] ${msg.username}: queued ${j.accepted ?? msg.items.length}`);
          sendResponse({ ok: r.ok });
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
            body: JSON.stringify({ platform: msg.platform || "instagram", source: msg.source, hop: msg.hop, discovered: msg.discovered }),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `discover[${msg.platform || "instagram"}] from ${msg.source} (hop ${msg.hop}): +${j.added ?? "?"} new`);
          sendResponse({ ok: r.ok });
        } catch (e) {
          await log("error", `discover failed: ${e.message}`);
          sendResponse({ ok: false, error: String(e) });
        }
        break;
      }
      case "posts": {  // structured post metadata (captions/likes/comments counts)
        try {
          const r = await fetch(base + "/social/posts", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform: msg.platform || "instagram", username: msg.username, posts: msg.posts }),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `posts[${msg.platform || "instagram"}] ${msg.username}: saved ${j.saved ?? "?"}`);
          sendResponse({ ok: r.ok });
        } catch (e) { await log("error", `posts failed: ${e.message}`); sendResponse({ ok: false }); }
        break;
      }
      case "comments": {  // comment threads
        try {
          const r = await fetch(base + "/social/comments", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform: msg.platform || "instagram", post_id: msg.post_id, comments: msg.comments }),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `comments[${msg.platform || "instagram"}] post ${msg.post_id}: saved ${j.saved ?? "?"}`);
          sendResponse({ ok: r.ok });
        } catch (e) { await log("error", `comments failed: ${e.message}`); sendResponse({ ok: false }); }
        break;
      }
      case "seed": {  // seed the spider from your own followers/following (hop 0)
        try {
          const r = await fetch(base + "/social/seed", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform: msg.platform || "instagram", users: msg.users }),
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
            body: JSON.stringify({ platform: msg.platform || "instagram", profile: msg.profile }),
          });
          sendResponse({ ok: r.ok });
        } catch (e) { sendResponse({ ok: false }); }
        break;
      }
      case "users": {  // universal user registry — anyone we encountered
        try {
          const r = await fetch(base + "/social/users", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform: msg.platform || "instagram", context: msg.context || "seen", users: msg.users }),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `users[${msg.platform || "instagram"}] +${j.recorded ?? "?"} (${msg.context || "seen"})`);
          sendResponse({ ok: r.ok });
        } catch (e) { await log("error", `users failed: ${e.message}`); sendResponse({ ok: false }); }
        break;
      }
      case "wall": {  // the in-tab loop hit a throttle/login wall and is sleeping
        const mins = msg.mins || 45;
        await setStatus({ cooldownUntil: Date.now() + mins * 60000 });
        await log("warn", `⚠️ ${msg.platform || "?"} throttle wall — loop sleeping ${mins}m`);
        sendResponse({ ok: true });
        break;
      }
      case "loopStatus": {  // continuous loop liveness ping
        await setStatus({ loopRunning: !!msg.running, loopPlatform: msg.platform, lastLoopPing: Date.now() });
        sendResponse({ ok: true });
        break;
      }
      case "tabReady": {  // scraper tab loaded; the loop auto-starts in the tab
        sendResponse({ ok: true });
        break;
      }
      case "log":
        log(msg.level || "info", `[${msg.platform || "?"}] ${msg.msg}`);
        sendResponse({ ok: true });
        break;
      case "cycleReport":
        setStatus({ lastCycleAt: Date.now(), lastCycle: { platform: msg.platform, targets: msg.targets, saved: msg.saved, discovered: msg.discovered } });
        log("info", `✅ cycle done [${msg.platform}]: ${msg.targets} targets, ${msg.saved} media, ${msg.discovered} discovered`);
        sendResponse({ ok: true });
        break;
      case "getPlatforms":
        sendResponse(await platformStatuses());
        break;
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
      case "scrapeNow":
        scrapeNow().then(sendResponse);
        return;
      default:
        sendResponse({ ok: false, error: "unknown message" });
    }
  })();
  return true; // async
});

async function scrapeNow() {
  await log("info", "manual 'Start/Resume loop' clicked");
  await setStatus({ cooldownUntil: 0 });
  return { ok: await ensureLoops("manual") };
}

// Warm start (worker waking from sleep)
setStatus({ swStartedAt: Date.now() });
log("info", "service worker active");
