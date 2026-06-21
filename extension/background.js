// UnifiedCollector Social Bridge — background service worker (MV3).
//
// MV3 service workers are EPHEMERAL: Chrome sleeps them after ~30s idle to save
// resources. They are not a persistent loop — instead a chrome.alarm wakes the
// worker every N minutes to run a scrape cycle (the content script on the social
// tab holds the logged-in session). So the bridge keeps working with the popup
// closed; it just isn't "always running". This file also keeps a persistent,
// storage-backed LOG ring buffer so the popup can show recent activity even after
// the worker has slept and restarted.

importScripts("platforms.js"); // defines globalThis.UC_PLATFORMS

const ALARM = "uc-scrape";
const DEFAULT_INGEST = "http://127.0.0.1:8765";
const LOG_KEY = "ucLog";
const LOG_MAX = 200;

// ---- persistent logging --------------------------------------------------
async function log(level, msg) {
  const entry = { t: Date.now(), level, msg };
  try {
    const { [LOG_KEY]: cur = [] } = await chrome.storage.local.get(LOG_KEY);
    cur.push(entry);
    while (cur.length > LOG_MAX) cur.shift();
    await chrome.storage.local.set({ [LOG_KEY]: cur });
  } catch (e) {
    /* storage busy */
  }
  console.log(`[UC ${level}] ${msg}`);
}

async function setStatus(patch) {
  const { ucStatus = {} } = await chrome.storage.local.get("ucStatus");
  await chrome.storage.local.set({ ucStatus: { ...ucStatus, ...patch } });
}

// ---- config --------------------------------------------------------------
async function ingestBase() {
  const { ingestBase } = await chrome.storage.local.get("ingestBase");
  return ingestBase || DEFAULT_INGEST;
}
async function intervalMinutes() {
  const { intervalMinutes } = await chrome.storage.local.get("intervalMinutes");
  return intervalMinutes || 30;
}

// ---- keep-alive via alarm ------------------------------------------------
async function scheduleAlarm() {
  const period = await intervalMinutes();
  chrome.alarms.create(ALARM, { periodInMinutes: period });
  await setStatus({ alarmPeriod: period, swStartedAt: Date.now() });
  await log("info", `worker started; scrape alarm every ${period} min`);
}

chrome.runtime.onInstalled.addListener(() => { scheduleAlarm(); log("info", "extension installed"); });
chrome.runtime.onStartup.addListener(() => { scheduleAlarm(); log("info", "browser startup"); });

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== ALARM) return;
  await log("info", "⏰ alarm woke worker → triggering scrape cycle");
  await setStatus({ lastAlarmAt: Date.now() });
  triggerScrape();
});

// Scrape-dispatch targets = platforms that actually have a content-script scraper.
function scraperUrlPatterns() {
  return (globalThis.UC_PLATFORMS || []).filter((p) => p.scraper).map((p) => `https://${p.host}/*`);
}

// ---- social tab launcher -------------------------------------------------
async function tabForHost(host) {
  const tabs = await chrome.tabs.query({ url: `*://${host}/*` });
  return tabs && tabs[0] ? tabs[0] : null;
}
async function isLoggedIn(p) {
  try {
    const c = await chrome.cookies.get({ url: p.cookieUrl, name: p.cookie });
    return !!(c && c.value);
  } catch (e) {
    return null; // unknown (no host permission / API hiccup)
  }
}
async function platformStatuses() {
  const out = [];
  for (const p of globalThis.UC_PLATFORMS || []) {
    const tab = await tabForHost(p.host);
    out.push({
      id: p.id, label: p.label, url: p.url, host: p.host, scraper: !!p.scraper,
      tabOpen: !!tab, tabId: tab ? tab.id : null, loggedIn: await isLoggedIn(p),
    });
  }
  return out;
}
async function openOrFocus(p, { pinned = true, active = false } = {}) {
  const tab = await tabForHost(p.host);
  if (tab) { await chrome.tabs.update(tab.id, { active }); return { id: p.id, focused: true }; }
  await chrome.tabs.create({ url: p.url, pinned, active });
  await log("info", `opened tab: ${p.label}`);
  return { id: p.id, opened: true };
}

async function triggerScrape() {
  const tabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
  if (tabs && tabs[0]) {
    chrome.tabs.sendMessage(tabs[0].id, { type: "scrapeCycle" });
    await log("info", `cycle dispatched to tab: ${new URL(tabs[0].url).host}`);
    return true;
  }
  await log("warn", "no supported social tab open — pin an instagram.com tab");
  return false;
}

// ---- message router ------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    const base = await ingestBase();
    switch (msg.type) {
      case "getTargets": {
        try {
          const r = await fetch(base + "/ig/targets");
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
          const r = await fetch(base + "/ig/ingest", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username: msg.username, items: msg.items }),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `ingest ${msg.username}: saved ${j.saved ?? "?"}/${msg.items.length}`);
          sendResponse({ ok: r.ok });
        } catch (e) {
          await log("error", `ingest ${msg.username} failed: ${e.message}`);
          sendResponse({ ok: false, error: String(e) });
        }
        break;
      }
      case "discover": {
        try {
          const r = await fetch(base + "/ig/discover", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source: msg.source, hop: msg.hop, discovered: msg.discovered }),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `discover from ${msg.source} (hop ${msg.hop}): +${j.added ?? "?"} new`);
          sendResponse({ ok: r.ok });
        } catch (e) {
          await log("error", `discover failed: ${e.message}`);
          sendResponse({ ok: false, error: String(e) });
        }
        break;
      }
      case "log":               // content script forwards a log line
        log(msg.level || "info", `[${msg.platform || "?"}] ${msg.msg}`);
        sendResponse({ ok: true });
        break;
      case "cycleReport":       // content script reports cycle stats
        setStatus({
          lastCycleAt: Date.now(),
          lastCycle: { platform: msg.platform, targets: msg.targets, saved: msg.saved, discovered: msg.discovered },
        });
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
        let opened = 0;
        for (const p of globalThis.UC_PLATFORMS || []) {
          const r = await openOrFocus(p, { active: false });
          if (r.opened) opened++;
        }
        await log("info", `open-all: launched ${opened} new tab(s)`);
        sendResponse({ opened });
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
  return { ok: await triggerScrape() };
}

// Warm start (worker waking from sleep): record it so the popup shows liveness.
setStatus({ swStartedAt: Date.now() });
log("info", "service worker active");
