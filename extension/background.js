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
const HEARTBEAT_MIN = 20;        // gentle heartbeat while a tab is open (human-paced)
const WALL_COOLDOWN_MIN = 30;    // back off this long after a throttle/review wall
const KICK_DEBOUNCE_MS = 60000;  // don't re-kick the same tab more often than this

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

// ---- scheduling (event-driven + heartbeat) -------------------------------
async function scheduleAlarm() {
  chrome.alarms.create(ALARM, { periodInMinutes: HEARTBEAT_MIN });
  await setStatus({ alarmPeriod: HEARTBEAT_MIN, swStartedAt: Date.now() });
  await log("info", `worker started; event-driven + ${HEARTBEAT_MIN}-min heartbeat`);
}
chrome.runtime.onInstalled.addListener(() => { scheduleAlarm(); kickIfReady("installed"); });
chrome.runtime.onStartup.addListener(() => { scheduleAlarm(); kickIfReady("startup"); });

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== ALARM) return;
  await kickIfReady("heartbeat");
});

// scraper hosts that have a content-script scraper
function scraperPlatforms() { return (globalThis.UC_PLATFORMS || []).filter((p) => p.scraper); }
function scraperUrlPatterns() { return scraperPlatforms().map((p) => `https://${p.host}/*`); }

async function inCooldown() {
  const s = await getStatus();
  return s.cooldownUntil && Date.now() < s.cooldownUntil;
}

// Dispatch a cycle to EVERY open scraper tab (one per platform runs in parallel;
// each tab self-guards against overlapping its own cycle).
async function triggerScrape(reason) {
  if (await inCooldown()) {
    const s = await getStatus();
    const mins = Math.round((s.cooldownUntil - Date.now()) / 60000);
    await log("info", `in cooldown ~${mins}m (${reason}) — skipping`);
    return false;
  }
  const tabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
  if (!tabs || !tabs.length) {
    await log("warn", `no scraper tab open — paused (${reason}). Open one via 🗂 Manage social tabs.`);
    return false;
  }
  await setStatus({ lastAlarmAt: Date.now() });
  for (const t of tabs) {
    try {
      await chrome.tabs.sendMessage(t.id, { type: "scrapeCycle" });
      await log("info", `cycle dispatched → ${new URL(t.url).host} (${reason})`);
    } catch (e) {
      // content script not injected yet (tab still loading) — ignore
    }
  }
  return true;
}

// debounced per-tab kick used by event triggers
const lastKick = {};
async function kickIfReady(reason, tabId) {
  if (tabId != null) {
    const now = Date.now();
    if (lastKick[tabId] && now - lastKick[tabId] < KICK_DEBOUNCE_MS) return;
    lastKick[tabId] = now;
  }
  return triggerScrape(reason);
}

// a scraper tab finished loading -> opportunity to scrape
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status !== "complete" || !tab.url) return;
  const host = (() => { try { return new URL(tab.url).host; } catch (e) { return ""; } })();
  if (scraperPlatforms().some((p) => host === p.host)) kickIfReady("tab-loaded", tabId);
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
      case "wall": {  // a content script hit a throttle/login wall -> back off
        const until = Date.now() + WALL_COOLDOWN_MIN * 60000;
        await setStatus({ cooldownUntil: until });
        await log("warn", `⚠️ ${msg.platform || "?"} throttle wall — cooling down ${WALL_COOLDOWN_MIN}m`);
        sendResponse({ ok: true });
        break;
      }
      case "tabReady": {  // scraper tab loaded -> scrape now (event-driven)
        sendResponse({ ok: true });
        if (sender.tab) kickIfReady("tab-ready", sender.tab.id);
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
  await log("info", "manual 'Scrape now' clicked");
  // manual override clears any active cooldown
  await setStatus({ cooldownUntil: 0 });
  return { ok: await triggerScrape("manual") };
}

// Warm start (worker waking from sleep)
setStatus({ swStartedAt: Date.now() });
log("info", "service worker active");
