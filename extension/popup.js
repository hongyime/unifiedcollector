const $ = (id) => document.getElementById(id);
const DEFAULT_INGEST = "http://127.0.0.1:8765";

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}
function hhmmss(ts) {
  const d = new Date(ts);
  return d.toLocaleTimeString([], { hour12: false });
}

async function ingestBase() {
  const { ingestBase } = await chrome.storage.local.get("ingestBase");
  return ingestBase || DEFAULT_INGEST;
}

async function renderStatus() {
  const { ucStatus = {}, intervalMinutes = 30 } = await chrome.storage.local.get(["ucStatus", "intervalMinutes"]);

  // background worker: the popup messaging it implies it's awake now
  $("sw").innerHTML = `<span class="dot ok"></span>active · started ${ago(ucStatus.swStartedAt)}`;

  // social tab present?
  const tabs = await chrome.tabs.query({ url: ["https://www.instagram.com/*"] });
  $("tab").innerHTML = tabs && tabs.length
    ? `<span class="dot ok"></span>instagram.com open`
    : `<span class="dot bad"></span>none open — pin one`;

  // ingest reachable?
  try {
    const base = await ingestBase();
    const ctrl = new AbortController();
    const to = setTimeout(() => ctrl.abort(), 2500);
    const r = await fetch(base + "/health", { signal: ctrl.signal });
    clearTimeout(to);
    $("ingestStatus").innerHTML = r.ok
      ? `<span class="dot ok"></span>connected`
      : `<span class="dot bad"></span>HTTP ${r.status}`;
  } catch (e) {
    $("ingestStatus").innerHTML = `<span class="dot bad"></span>unreachable`;
  }

  // cycle cadence + next wake estimate
  const period = ucStatus.alarmPeriod || intervalMinutes;
  let next = "";
  if (ucStatus.lastAlarmAt) {
    const due = ucStatus.lastAlarmAt + period * 60000;
    const mins = Math.max(0, Math.round((due - Date.now()) / 60000));
    next = ` · next ~${mins}m`;
  }
  $("cycle").textContent = `every ${period}m${next}`;

  // last cycle result
  if (ucStatus.lastCycle) {
    const c = ucStatus.lastCycle;
    $("last").textContent = `${ago(ucStatus.lastCycleAt)} · ${c.saved} media, ${c.discovered} found`;
  } else {
    $("last").textContent = "no cycle yet";
  }

  $("keepalive").textContent =
    "The background worker sleeps to save resources; a Chrome alarm wakes it every " +
    period + " min to run a scrape cycle — so it keeps working with this popup closed.";
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
function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

async function refresh() { await renderStatus(); await renderLog(); }

async function load() {
  const { ingestBase, intervalMinutes } = await chrome.storage.local.get(["ingestBase", "intervalMinutes"]);
  $("ingest").value = ingestBase || DEFAULT_INGEST;
  $("interval").value = intervalMinutes || 30;
  await refresh();
}

$("save").addEventListener("click", async () => {
  const ingestBase = $("ingest").value.trim() || DEFAULT_INGEST;
  const intervalMinutes = parseInt($("interval").value, 10) || 30;
  await chrome.storage.local.set({ ingestBase, intervalMinutes });
  chrome.alarms.create("uc-scrape", { periodInMinutes: intervalMinutes });
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

load();
setInterval(refresh, 1500);
