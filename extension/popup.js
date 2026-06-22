const $ = (id) => document.getElementById(id);
const DEFAULT_INGEST = "http://127.0.0.1:8765";

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

  $("sw").innerHTML = `<span class="dot ok"></span>active · ${ago(ucStatus.swStartedAt)}`;

  const tabs = await chrome.tabs.query({
    url: ["https://www.instagram.com/*", "https://www.tiktok.com/*", "https://www.lemon8-app.com/*",
          "https://x.com/*", "https://www.threads.com/*", "https://www.facebook.com/*"],
  });
  $("tab").innerHTML = tabs && tabs.length
    ? `<span class="dot ok"></span>${tabs.length} scraper tab(s)`
    : `<span class="dot bad"></span>none open — pin one`;

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

  // continuous loop status (the loop pings loopStatus while alive)
  const fresh = ucStatus.lastLoopPing && Date.now() - ucStatus.lastLoopPing < 8 * 60000;
  if (ucStatus.cooldownUntil && Date.now() < ucStatus.cooldownUntil) {
    const m = Math.round((ucStatus.cooldownUntil - Date.now()) / 60000);
    $("loop").innerHTML = `<span class="dot idle"></span>cooling down ~${m}m (wall)`;
  } else if (ucStatus.loopRunning && fresh) {
    $("loop").innerHTML = `<span class="dot ok"></span>running · ${ucStatus.loopPlatform || ""} · ${ago(ucStatus.lastLoopPing)}`;
  } else {
    $("loop").innerHTML = `<span class="dot bad"></span>not running — open a tab / hit Start`;
  }

  if (ucStatus.lastCycle) {
    const c = ucStatus.lastCycle;
    $("last").textContent = `${ago(ucStatus.lastCycleAt)} · ${c.saved} media, ${c.discovered} found`;
  } else {
    $("last").textContent = "no pass yet";
  }

  $("keepalive").textContent =
    "The work runs as one continuous loop inside your open social tab — rate-limited and jittered, no fixed timer. " +
    "A 10-min watchdog respawns it if the tab reloads or the worker slept.";
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
  const { ingestBase, ucConfig = {} } = await chrome.storage.local.get(["ingestBase", "ucConfig"]);
  $("ingest").value = ingestBase || DEFAULT_INGEST;
  const cfg = { stories: true, highlights: true, comments: true, ...ucConfig };
  $("cfgStories").checked = cfg.stories;
  $("cfgHighlights").checked = cfg.highlights;
  $("cfgComments").checked = cfg.comments;
  await refresh();
}

async function saveConfig() {
  await chrome.storage.local.set({
    ucConfig: { stories: $("cfgStories").checked, highlights: $("cfgHighlights").checked, comments: $("cfgComments").checked },
  });
}
["cfgStories", "cfgHighlights", "cfgComments"].forEach((id) => {
  $(id).addEventListener("change", saveConfig);
});

$("save").addEventListener("click", async () => {
  const ingestBase = $("ingest").value.trim() || DEFAULT_INGEST;
  await chrome.storage.local.set({ ingestBase });
  refresh();
});

$("scrape").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "scrapeNow" });
  setTimeout(refresh, 500);
});

$("clear").addEventListener("click", async () => {
  await chrome.storage.local.set({ ucLog: [] });
  renderLog();
});

// Copy the full log to the clipboard — the popup re-renders every 1.5s, which
// wipes any manual text selection before Ctrl+C lands, so a button is the
// reliable way to grab logs.
$("copy").addEventListener("click", async () => {
  const { ucLog = [] } = await chrome.storage.local.get("ucLog");
  const text = ucLog.map((e) => `${hhmmss(e.t)} [${e.level}] ${e.msg}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    $("copy").textContent = "copied!";
  } catch (e) {
    // clipboard API can be blocked in popups — fall back to a textarea + execCommand
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); $("copy").textContent = "copied!"; }
    catch (e2) { $("copy").textContent = "copy failed"; }
    ta.remove();
  }
  setTimeout(() => ($("copy").textContent = "copy"), 1500);
});

$("tabsBtn").addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("tabs.html") });
});

load();
setInterval(refresh, 1500);
