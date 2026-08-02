const $ = (id) => document.getElementById(id);
let lastDiag = null;
let lastProbe = null;
let lastAutoProbeAt = 0;
let autoProbeInFlight = false;

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor((Date.now() - Number(ts)) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function hhmmss(ts) {
  try { return new Date(ts).toLocaleTimeString([], { hour12: false }); }
  catch (e) { return "--:--:--"; }
}

function runtimeErrorText(e) {
  return chrome.runtime.lastError && chrome.runtime.lastError.message
    ? chrome.runtime.lastError.message
    : String(e && e.message ? e.message : e);
}

function messageTimeoutMs(msg) {
  const type = msg && msg.type;
  if (type === "refreshScraperTabs") return 180000;
  if (type === "openAll" || type === "scrapeNow") return 60000;
  if (type === "testIngest") return 45000;
  if (type === "getPlatforms" || type === "diagnostics") return 15000;
  return 10000;
}

function sendMessage(msg) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`background worker response timed out after ${messageTimeoutMs(msg)}ms`)), messageTimeoutMs(msg));
    try {
      chrome.runtime.sendMessage(msg, (response) => {
        clearTimeout(timer);
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(response);
      });
    } catch (e) {
      clearTimeout(timer);
      reject(e);
    }
  });
}

function badge(label, state, text) {
  // state: ok | bad | warn | idle
  return `<span class="badge"><span class="dot ${state}"></span>${text}</span>`;
}

async function render() {
  let platforms = [];
  let backgroundError = null;
  try {
    platforms = (await sendMessage({ type: "getPlatforms" })) || [];
    lastDiag = await sendMessage({ type: "diagnostics" });
  } catch (e) {
    backgroundError = runtimeErrorText(e);
  }
  renderDiagnostics(backgroundError);
  const list = $("list");
  list.innerHTML = platforms
    .map((p) => {
      const login = p.noLogin ? badge("login", "idle", "no login needed")
        : p.loggedIn === true ? badge("login", "ok", "logged in")
        : p.loggedIn === false ? badge("login", "bad", "not logged in")
        : badge("login", "idle", "login unknown");
      const tab = p.tabOpen ? badge("tab", "ok", "tab open") : badge("tab", "idle", "no tab");
      const scr = p.scraper
        ? `<span class="pill active">scraper active</span>`
        : `<span class="pill">login-ready</span>`;
      return `
        <div class="card">
          <div>
            <div class="name">${p.label} ${scr}</div>
            <div class="host">${p.host}</div>
          </div>
          <div class="badges">
            ${tab}
            ${login}
            <button class="open" data-id="${p.id}">${p.tabOpen ? "Focus" : "Open"}</button>
          </div>
        </div>`;
    })
    .join("");

  list.querySelectorAll("button.open").forEach((b) => {
    b.addEventListener("click", async () => {
      await sendMessage({ type: "openPlatform", id: b.dataset.id });
      setTimeout(render, 400);
    });
  });

  const scrapers = platforms.filter((p) => p.scraper).map((p) => p.label).join(", ") || "none yet";
  $("note").innerHTML =
    `<b>Scrapers active:</b> ${scrapers}. Other platforms open for login now and will scrape ` +
    `automatically once their scraper is added. Tabs open <b>pinned</b> so they persist; ` +
    `keep them logged in. The bridge runs a scrape cycle on a timer (see the popup).`;
  maybeAutoProbe(backgroundError);
}

function renderDiagnostics(backgroundError) {
  const el = $("diag");
  if (!el) return;
  const pageVersion = chrome.runtime.getManifest ? chrome.runtime.getManifest().version : "?";
  if (backgroundError) {
    el.innerHTML =
      `<strong class="bad">Background worker unreachable.</strong><br>` +
      `Options page version: ${escapeHtml(pageVersion)}. ` +
      `Chrome error: ${escapeHtml(backgroundError)}<br>` +
      `Use Reload extension, then reopen this page. If this stays red, Chrome did not start the MV3 service worker.`;
    return;
  }
  const status = (lastDiag && lastDiag.status) || {};
  const logs = ((lastDiag && lastDiag.log) || [])
    .slice(-8)
    .map((e) => `${hhmmss(e.t)} [${e.level}] ${e.msg}`)
    .join("\n");
  const workerFresh = status.swStartedAt && Date.now() - Number(status.swStartedAt) < 10 * 60 * 1000;
  const probe = lastProbe
    ? lastProbe.ok
      ? `<span class="ok">${lastProbe.auto ? "auto" : "manual"} ingest ok</span> (${escapeHtml(lastProbe.base || "")})`
      : `<span class="bad">${lastProbe.auto ? "auto" : "manual"} ingest failed</span>: ${escapeHtml(lastProbe.error || "unknown")}`
    : "manual ingest not tested from this page";
  const bridgeHeartbeat = status.lastBridgeHeartbeatOkAt
    ? `bridge heartbeat ok ${escapeHtml(ago(status.lastBridgeHeartbeatOkAt))}`
    : "bridge heartbeat not confirmed";
  const bridgeProblem = status.lastBridgeHeartbeatError
    ? `<br><span class="bad">Bridge heartbeat error:</span> ${escapeHtml(status.lastBridgeHeartbeatError)}`
    : "";
  const scraperHeartbeat = status.lastScraperHeartbeatAt
    ? `scraper heartbeat ${escapeHtml(ago(status.lastScraperHeartbeatAt))}: ${Number(status.lastScraperHeartbeatSent || 0)}/${Number(status.lastScraperHeartbeatSeen || 0)} sent`
    : "scraper heartbeat not confirmed";
  el.innerHTML =
    `<strong>Extension ${escapeHtml((lastDiag && lastDiag.version) || pageVersion)}</strong> ` +
    `<span class="${workerFresh ? "ok" : "warn"}">worker ${workerFresh ? "fresh" : "not fresh"}</span><br>` +
    `Worker started ${escapeHtml(ago(status.swStartedAt))}; loop ping ${escapeHtml(ago(status.lastLoopPing))}; ` +
    `ingest ${escapeHtml((lastDiag && lastDiag.ingestBase) || "")}<br>` +
    `${bridgeHeartbeat}; ${scraperHeartbeat}${bridgeProblem}<br>` +
    `Probe: ${probe}` +
    (logs ? `<pre>${escapeHtml(logs)}</pre>` : "");
}

async function maybeAutoProbe(backgroundError) {
  if (backgroundError || autoProbeInFlight) return;
  const now = Date.now();
  if (now - lastAutoProbeAt < 120000) return;
  lastAutoProbeAt = now;
  autoProbeInFlight = true;
  try {
    const result = await sendMessage({ type: "testIngest" });
    lastProbe = { ...(result || {}), auto: true, t: Date.now() };
  } catch (e) {
    lastProbe = { ok: false, error: runtimeErrorText(e), auto: true, t: Date.now() };
  } finally {
    autoProbeInFlight = false;
    renderDiagnostics(null);
  }
}

$("openAll").addEventListener("click", async () => {
  await sendMessage({ type: "openAll" });
  setTimeout(render, 800);
});
$("refresh").addEventListener("click", render);
$("refreshTabs").addEventListener("click", async () => {
  await sendMessage({ type: "refreshScraperTabs", reason: "tabs_page_button" });
  setTimeout(render, 1000);
});
$("scrape").addEventListener("click", async () => {
  await sendMessage({ type: "scrapeNow" });
});
$("reloadExtension").addEventListener("click", reloadExtension);
$("testIngest").addEventListener("click", async () => {
  lastProbe = { ok: false, error: "running..." };
  renderDiagnostics(null);
  try {
    lastProbe = { ...((await sendMessage({ type: "testIngest" })) || {}), auto: false, t: Date.now() };
  } catch (e) {
    lastProbe = { ok: false, error: runtimeErrorText(e), auto: false, t: Date.now() };
  }
  await render();
});

async function reloadExtension(options = {}) {
  try {
    await sendMessage({ type: "log", level: "info", msg: "manual extension reload requested" });
  } catch (e) {}
  try {
    await chrome.storage.local.set({
      ucReloadIntent: {
        requested_at: Date.now(),
        source: "tabs_page",
        force_open_all: options.force_open_all === true,
        force_refresh_tabs: options.force_refresh_tabs !== false,
        force_scrape: !!options.force_scrape,
        force_test: !!options.force_test,
        open_ids: Array.isArray(options.open_ids) ? options.open_ids.slice(0, 20) : [],
      },
    });
  } catch (e) {}
  try {
    history.replaceState(null, "", location.pathname);
  } catch (e) {}
  chrome.runtime.reload();
}

async function handleUrlActions() {
  let params;
  try {
    params = new URL(location.href).searchParams;
  } catch (e) {
    return;
  }
  const shouldOpenAll = params.get("openAll") === "1";
  const shouldReload = params.get("reload") === "1";
  const openIds = (params.get("open") || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const shouldRefreshTabs = params.get("refreshTabs") === "1";
  const shouldScrape = params.get("scrape") === "1";
  const shouldTest = params.get("test") === "1";
  if (!shouldReload && !shouldOpenAll && !openIds.length && !shouldRefreshTabs && !shouldScrape && !shouldTest) return;
  try {
    history.replaceState(null, "", location.pathname);
  } catch (e) {}
  if (shouldReload) {
    await reloadExtension({
      force_open_all: shouldOpenAll,
      force_refresh_tabs: shouldRefreshTabs || shouldOpenAll || !openIds.length || openIds.length > 0,
      force_scrape: shouldScrape,
      force_test: shouldTest,
      open_ids: openIds,
    });
    return;
  }
  try {
    if (shouldOpenAll) {
      await sendMessage({ type: "openAll" });
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
    for (const id of openIds) {
      await sendMessage({ type: "openPlatform", id });
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
    if (shouldRefreshTabs) {
      await sendMessage({ type: "refreshScraperTabs", reason: "tabs_page_url" });
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
    if (shouldScrape) {
      await sendMessage({ type: "scrapeNow" });
    }
    if (shouldTest) {
      lastProbe = { ...((await sendMessage({ type: "testIngest" })) || {}), auto: false, t: Date.now() };
    }
  } catch (e) {
    lastProbe = { ok: false, error: runtimeErrorText(e), auto: false, t: Date.now() };
  }
  setTimeout(render, 1000);
}

render();
handleUrlActions();
setInterval(render, 3000);
