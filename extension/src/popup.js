const $ = (id) => document.getElementById(id);
const DEFAULT_INGEST = "http://127.0.0.1:8765";
const SCRAPER_URLS = [
  "https://www.instagram.com/*", "https://www.tiktok.com/*", "https://www.lemon8-app.com/*",
  "https://x.com/*", "https://www.threads.com/*", "https://www.facebook.com/*",
];

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}
function hhmmss(ts) { return new Date(ts).toLocaleTimeString([], { hour12: false }); }

async function ingestBase() {
  const { ingestBase } = await chrome.storage.local.get("ingestBase");
  return ingestBase || DEFAULT_INGEST;
}

async function renderStatus() {
  const { ucStatus = {} } = await chrome.storage.local.get("ucStatus");
  const tabs = await chrome.tabs.query({ url: SCRAPER_URLS });
  const nTabs = tabs ? tabs.length : 0;

  $("tab").innerHTML = nTabs
    ? `<span class="dot ok"></span>${nTabs} open`
    : `<span class="dot bad"></span>none — open one below`;

  // collector reachable?
  try {
    const base = await ingestBase();
    const ctrl = new AbortController();
    const to = setTimeout(() => ctrl.abort(), 2500);
    const r = await fetch(base + "/health", { signal: ctrl.signal });
    clearTimeout(to);
    $("ingestStatus").innerHTML = r.ok ? `<span class="dot ok"></span>connected` : `<span class="dot bad"></span>HTTP ${r.status}`;
  } catch (e) {
    $("ingestStatus").innerHTML = `<span class="dot bad"></span>unreachable`;
  }

  // ONE clear scraping state (no "loop/worker" jargon)
  const fresh = ucStatus.lastLoopPing && Date.now() - ucStatus.lastLoopPing < 8 * 60000;
  if (!nTabs) {
    $("scrapeState").innerHTML = `<span class="dot idle"></span>paused — open a social tab`;
  } else if (ucStatus.cooldownUntil && Date.now() < ucStatus.cooldownUntil) {
    const m = Math.max(1, Math.round((ucStatus.cooldownUntil - Date.now()) / 60000));
    $("scrapeState").innerHTML = `<span class="dot warn"></span>cooling down ~${m}m (rate-limit)`;
  } else if (ucStatus.loopRunning && fresh) {
    $("scrapeState").innerHTML = `<span class="dot ok"></span>running${ucStatus.loopPlatform ? " · " + ucStatus.loopPlatform : ""}`;
  } else {
    $("scrapeState").innerHTML = `<span class="dot warn"></span>starting… keep the tab open`;
  }

  $("last").textContent = ucStatus.lastCycle
    ? `${ago(ucStatus.lastCycleAt)} · ${ucStatus.lastCycle.saved} media candidate(s), ${ucStatus.lastCycle.discovered} found`
    : "—";

  $("keepalive").textContent =
    "Scraping runs continuously inside your open, logged-in social tabs (rate-limited + jittered). " +
    "It starts on its own and pauses when no tab is open — nothing to press.";
}

async function renderLog() {
  const { ucLog = [] } = await chrome.storage.local.get("ucLog");
  const el = $("log");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  el.innerHTML = ucLog
    .slice(-120)
    .map((e) => `<div class="line"><span class="t">${hhmmss(e.t)}</span> <span class="${e.level}">${escapeHtml(e.msg)}</span></div>`)
    .join("");
  if (atBottom) el.scrollTop = el.scrollHeight;
}
function escapeHtml(s) { return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

async function refresh() { await renderStatus(); await renderLog(); }

async function load() {
  const { ingestBase } = await chrome.storage.local.get(["ingestBase"]);
  $("ingest").value = ingestBase || DEFAULT_INGEST;
  await refresh();
}

$("save").addEventListener("click", async () => {
  const ingestBase = $("ingest").value.trim() || DEFAULT_INGEST;
  await chrome.storage.local.set({ ingestBase });
  refresh();
});

$("clear").addEventListener("click", async () => {
  await chrome.storage.local.set({ ucLog: [] });
  renderLog();
});

$("copy").addEventListener("click", async () => {
  const { ucLog = [] } = await chrome.storage.local.get("ucLog");
  const text = ucLog.map((e) => `${hhmmss(e.t)} [${e.level}] ${e.msg}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    $("copy").textContent = "copied!";
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); $("copy").textContent = "copied!"; }
    catch (e2) { $("copy").textContent = "copy failed"; }
    ta.remove();
  }
  setTimeout(() => ($("copy").textContent = "copy"), 1500);
});

async function openControlTabsPage() {
  const url = chrome.runtime.getURL("tabs.html");
  try {
    const tabs = await chrome.tabs.query({});
    const existing = (tabs || []).find((tab) => String(tab.url || "").startsWith(url));
    if (existing && existing.id != null) {
      await chrome.tabs.update(existing.id, { active: true, pinned: false });
      if (existing.windowId != null) {
        await chrome.windows.update(existing.windowId, { focused: true }).catch(() => {});
      }
      return;
    }
  } catch (e) {}
  await chrome.tabs.create({ url, pinned: false });
}

$("tabsBtn").addEventListener("click", () => {
  openControlTabsPage().catch(() => {});
});

load();
setInterval(refresh, 1500);
