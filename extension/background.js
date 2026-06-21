// UnifiedCollector IG Bridge — background service worker (MV3).
// MV3 service workers are ephemeral, so we use chrome.alarms to periodically
// nudge the content script (which holds the IG session) to run a scrape cycle,
// and we relay scraped media to the local collector ingest endpoint. The SW can
// fetch localhost cross-origin thanks to host_permissions in the manifest.

const ALARM = "ig-scrape";

// Default ingest endpoint; override from the popup (stored in chrome.storage).
const DEFAULT_INGEST = "http://127.0.0.1:8765";

async function ingestBase() {
  const { ingestBase } = await chrome.storage.local.get("ingestBase");
  return ingestBase || DEFAULT_INGEST;
}

async function intervalMinutes() {
  const { intervalMinutes } = await chrome.storage.local.get("intervalMinutes");
  return intervalMinutes || 30;
}

async function scheduleAlarm() {
  const period = await intervalMinutes();
  chrome.alarms.create(ALARM, { periodInMinutes: period });
}

chrome.runtime.onInstalled.addListener(scheduleAlarm);
chrome.runtime.onStartup.addListener(scheduleAlarm);

async function triggerScrape() {
  const tabs = await chrome.tabs.query({ url: "https://www.instagram.com/*" });
  if (tabs && tabs[0]) {
    chrome.tabs.sendMessage(tabs[0].id, { type: "scrapeCycle" });
    return true;
  }
  console.warn("[IG Bridge] no instagram.com tab open — keep one open/pinned");
  return false;
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === ALARM) triggerScrape();
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    const base = await ingestBase();
    if (msg.type === "getTargets") {
      try {
        const r = await fetch(base + "/ig/targets");
        const j = await r.json();
        sendResponse(j.targets || []);
      } catch (e) {
        sendResponse([]);
      }
    } else if (msg.type === "ingest") {
      try {
        const r = await fetch(base + "/ig/ingest", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: msg.username, items: msg.items }),
        });
        sendResponse({ ok: r.ok });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    } else if (msg.type === "discover") {
      try {
        const r = await fetch(base + "/ig/discover", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source: msg.source, hop: msg.hop, discovered: msg.discovered }),
        });
        sendResponse({ ok: r.ok });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    } else if (msg.type === "scrapeNow") {
      sendResponse({ ok: await triggerScrape() });
    }
  })();
  return true; // async
});
