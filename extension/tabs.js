const $ = (id) => document.getElementById(id);
let lastDiag = null;
let lastProbe = null;

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

function sendMessage(msg) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendMessage(msg, (response) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(response);
      });
    } catch (e) {
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
      ? `<span class="ok">manual ingest ok</span> (${escapeHtml(lastProbe.base || "")})`
      : `<span class="bad">manual ingest failed</span>: ${escapeHtml(lastProbe.error || "unknown")}`
    : "manual ingest not tested from this page";
  el.innerHTML =
    `<strong>Extension ${escapeHtml((lastDiag && lastDiag.version) || pageVersion)}</strong> ` +
    `<span class="${workerFresh ? "ok" : "warn"}">worker ${workerFresh ? "fresh" : "not fresh"}</span><br>` +
    `Worker started ${escapeHtml(ago(status.swStartedAt))}; loop ping ${escapeHtml(ago(status.lastLoopPing))}; ` +
    `ingest ${escapeHtml((lastDiag && lastDiag.ingestBase) || "")}<br>` +
    `Probe: ${probe}` +
    (logs ? `<pre>${escapeHtml(logs)}</pre>` : "");
}

$("openAll").addEventListener("click", async () => {
  await sendMessage({ type: "openAll" });
  setTimeout(render, 800);
});
$("refresh").addEventListener("click", render);
$("scrape").addEventListener("click", async () => {
  await sendMessage({ type: "scrapeNow" });
});
$("reloadExtension").addEventListener("click", reloadExtension);
$("testIngest").addEventListener("click", async () => {
  lastProbe = { ok: false, error: "running..." };
  renderDiagnostics(null);
  try {
    lastProbe = await sendMessage({ type: "testIngest" });
  } catch (e) {
    lastProbe = { ok: false, error: runtimeErrorText(e) };
  }
  await render();
});

async function reloadExtension() {
  try {
    await sendMessage({ type: "log", level: "info", msg: "manual extension reload requested" });
  } catch (e) {}
  try {
    await chrome.storage.local.set({ ucReloadIntent: { requested_at: Date.now(), source: "tabs_page" } });
  } catch (e) {}
  try {
    history.replaceState(null, "", location.pathname);
  } catch (e) {}
  chrome.runtime.reload();
}

try {
  if (new URL(location.href).searchParams.get("reload") === "1") {
    setTimeout(reloadExtension, 600);
  }
} catch (e) {}

render();
setInterval(render, 3000);
