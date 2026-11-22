// @ts-nocheck — background.ts is the second-largest bundle entry (~2900
// LOC). Per docs/plans/extension-bundler.md step 15 we keep bodies
// unchecked at the initial TS conversion so import shapes are strictly
// typed (validated by shared/*.ts) without blocking the rollout on a
// full-strict pass. A follow-up pass will remove @ts-nocheck file-by-file.

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

import { hasActiveBrowserMediaRevisit, openOrFocus, shouldBrowserUploadMedia, uploadBrowserMediaCandidates } from "./background/browser_upload.js";
import { SCRAPER_HEARTBEAT_TIMEOUT_MS, extensionVersion, reportScraperTabHeartbeats, sendManualIngestProbe, withExtensionVersion } from "./background/heartbeat.js";
import { ensureLoops, kick, platformStatuses } from "./background/loops.js";
import { checkMemorySensitiveTabs } from "./background/memory.js";
import { PAGE_RECOVERY_PREFIX, clearPageRecoveryForTab, platformById, platformHosts, recordPageHealth, runPageRecovery, schedulePageRecovery, scraperPlatforms } from "./background/page_recovery.js";
import { BROWSER_REVISIT_WATCHDOG_PREFIX, armBrowserRevisitWatchdog, browserRevisitPageMatches, clearBrowserRevisitWatchdog, runBrowserRevisitWatchdog } from "./background/revisit_watchdog.js";
import { browserPacing } from "./background/scrape_config.js";
import { DEFAULT_INGEST, bridgeHeaders, controlBase, ingestBase, postJsonWithTimeout } from "./shared/ingest_client.js";
import { LOG_KEY, log } from "./shared/log.js";
import { UC_PLATFORMS } from "./shared/platforms.js";
import { ALARM, ALARM_MEMORY, ALARM_REFRESH, runStartupRecovery } from "./background/startup.js";
import { setStatus } from "./background/status.js";
import { EXTENSION_AUTO_RELOAD_ALARM, maybeForceScrapeCycle } from "./background/tab_recovery.js";
import { assertBrowserTabBudget, ensureScraperTabsOpen, refreshScraperTabs, scraperTabSelection } from "./background/tab_selection.js";
import { _alarmJitter, _sleep } from "./background/util.js";

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

async function _reportSwCrash(detail) {
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
      headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
      body: JSON.stringify({
        ...detail,
        extension_version: (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || null,
        reported_at: Date.now(),
      }),
      keepalive: true,
    }).catch(() => {});
  } catch (_) {}
}

// Assign to globalThis so existing call sites keep working unchanged. The
// registry itself lives in src/shared/platforms.js — imported at build time
// by esbuild, inlined into dist/background.js. No runtime importScripts /
// dynamic module load ever runs, so the historical failure mode ("failed
// importScripts prevents every alarm/listener from registering") is
// structurally impossible with the bundler.
globalThis.UC_PLATFORMS = UC_PLATFORMS;

const RELOAD_INTENT_KEY = "ucReloadIntent";

const LOADED_VERSION_KEY = "ucLoadedExtensionVersion";

async function fetchJsonWithTimeout(url, timeoutMs = 12000, headers = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ctrl.signal, headers });
    const j = await r.json().catch(() => ({}));
    return { response: r, json: j };
  } finally {
    clearTimeout(timer);
  }
}

// onInstalled fires on every extension reload/update — the exact moment content
// scripts in already-open tabs get SEVERED ("Extension context invalidated") and go
// silent. Messaging them (ensureLoops) can't revive a severed script, so we RELOAD
// the scraper tabs to respawn fresh content scripts immediately, instead of leaving
// them dead until the 75-min auto-refresh. This is the "I reloaded the extension and
// scraping stopped" fix.
chrome.runtime.onInstalled.addListener(async () => {
  // Keep exactly one canonical tab per platform; startup recovery removes any
  // tabs left over from older expanded-tab versions.
  runStartupRecovery("installed", { force: true, refreshTabs: true, refreshGapMs: 1500, retries: 2 });
});

chrome.runtime.onStartup.addListener(() => {
  runStartupRecovery("startup", { force: false, refreshTabs: true, refreshGapMs: 1500, retries: 2 });
});

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name && a.name.startsWith(BROWSER_REVISIT_WATCHDOG_PREFIX)) { await runBrowserRevisitWatchdog(a.name); }
  else if (a.name && a.name.startsWith(PAGE_RECOVERY_PREFIX)) { await runPageRecovery(a.name.slice(PAGE_RECOVERY_PREFIX.length)); }
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
  }
  else if (a.name === ALARM_MEMORY) {
    await _alarmJitter();
    await checkMemorySensitiveTabs("scheduled");
  }
});

// a scraper tab finished loading -> make sure its loop is running
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status !== "complete" || !tab.url) return;
  const host = (() => { try { return new URL(tab.url).host; } catch (e) { return ""; } })();
  if (scraperPlatforms().some((p) => platformHosts(p).includes(host))) kick("tab-loaded", tabId);
});

// ---- message router ------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "getBrowserTabId") {
    const tabId = sender && sender.tab && Number.isInteger(sender.tab.id)
      ? sender.tab.id
      : null;
    sendResponse({ ok: true, tab_id: tabId });
    return false;
  }
  if (msg && msg.type === "getBrowserRevisitTabState") {
    const tabId = Number(msg.tab_id);
    if (!Number.isInteger(tabId) || tabId < 0) {
      sendResponse({ ok: true, exists: false, at_target: false });
      return false;
    }
    chrome.tabs.get(tabId, (tab) => {
      const missing = Boolean(chrome.runtime.lastError) || !tab;
      sendResponse({
        ok: true,
        exists: !missing,
        at_target: !missing && browserRevisitPageMatches(
          String(msg.platform || "").toLowerCase(),
          tab.url || tab.pendingUrl || "",
          String(msg.url || ""),
        ),
      });
    });
    return true;
  }
  // Fast-path for high-frequency fire-and-forget message types: reply
  // synchronously BEFORE any await, so a cold-start of the service worker
  // (which pays for a chrome.storage.local.get in ingestBase()) can't push
  // us past the content-script's 10-15 s deadline. All these callers use
  // .catch(()=>{}) and never consume the return value, and log() /
  // setStatus() / postJsonWithTimeout() etc. keep the SW alive on their own.
  const fastReplyTypes = new Set(["log", "tabReady", "loopStatus", "cycleReport"]);
  if (msg && fastReplyTypes.has(msg.type)) {
    sendResponse({ ok: true });
    // Continue heavy work asynchronously after closing the response channel.
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
      case "browserPacing": {
        try {
          sendResponse(await browserPacing(msg.platform, msg.account));
        } catch (e) {
          sendResponse({ mode: "near_live_polling", volume_profile: "normal", fail_open: true });
        }
        break;
      }
      case "recoveryCleanPass": {
        try {
          const response = await fetch(base + "/social/recovery-clean-pass", {
            method: "POST",
            headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
            body: JSON.stringify({ source: msg.platform, account: msg.account || null }),
          });
          sendResponse(await response.json().catch(() => ({ ok: false })));
        } catch (e) {
          sendResponse({ ok: false, error: String(e && e.message || e) });
        }
        break;
      }
      case "igCooldown": {
        // Ask the bridge whether the HEADLESS collector is in a 429 cooldown so the
        // extension can rest in sync (shared IG account → cooperative anti-ban).
        try {
          const account = String(msg.account || msg.owner || "").trim();
          const qs = account ? `?account=${encodeURIComponent(account)}` : "";
          const r = await fetch(base + "/social/ig_cooldown" + qs, { headers: await bridgeHeaders() });
          sendResponse(await r.json());
        } catch (e) {
          sendResponse({ cooling: false, secs_left: 0, streak: 0 });
        }
        break;
      }
      case "getTargets": {
        try {
          const r = await fetch(base + `/social/targets?platform=${encodeURIComponent(msg.platform || "instagram")}`, { headers: await bridgeHeaders() });
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
          const r = await fetch(base + "/social/x-profile-target" + qs, { headers: await bridgeHeaders() });
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
            headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
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
          const r = await fetch(base + "/social/tiktok-revisit-target" + qs, { headers: await bridgeHeaders() });
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, target: null, error: String(e.message || e) });
        }
        break;
      }
      case "browserRevisitNavigationStarted": {
        sendResponse(await armBrowserRevisitWatchdog(msg, sender));
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
          const r = await fetch(base + "/social/browser-revisit-target" + qs, { headers: await bridgeHeaders() });
          const j = await r.json().catch(() => ({}));
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, target: null, error: String(e.message || e) });
        }
        break;
      }
      case "getNativeScrapeTarget": {
        try {
          const platform = String(msg.platform || "").trim().toLowerCase();
          const qs = new URLSearchParams({ platform });
          const r = await fetch(base + "/social/native-scrape-target?" + qs.toString(), { headers: await bridgeHeaders() });
          const j = await r.json().catch(() => ({}));
          const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
          if (r.ok && tabId !== null && j.target && j.target.id != null) {
            await chrome.storage.local.set({
              [`ucNativeScrapeTarget:${tabId}`]: {
                platform,
                id: Number(j.target.id),
                url: String(j.target.target_url || ""),
                username: j.target.username || null,
              },
            });
          }
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, target: null, error: String(e.message || e) });
        }
        break;
      }
      case "getNativeScrapeState": {
        try {
          const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
          if (tabId === null) {
            sendResponse({ ok: true, target: null });
            break;
          }
          const key = `ucNativeScrapeTarget:${tabId}`;
          const stored = await chrome.storage.local.get([key]);
          const target = stored && stored[key] ? stored[key] : null;
          if (target && msg.platform && target.platform !== String(msg.platform).toLowerCase()) {
            await chrome.storage.local.remove([key]);
            sendResponse({ ok: true, target: null });
          } else {
            sendResponse({ ok: true, target });
          }
        } catch (e) {
          sendResponse({ ok: false, target: null, error: String(e.message || e) });
        }
        break;
      }
      case "clearNativeScrapeState": {
        try {
          const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
          if (tabId !== null) await chrome.storage.local.remove([`ucNativeScrapeTarget:${tabId}`]);
          sendResponse({ ok: true });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "nativeScrape": {
        try {
          const r = await fetch(base + "/social/native-scrape", {
            method: "POST",
            headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform,
              target_id: msg.target_id ?? null,
              page_url: msg.page_url,
              username: msg.username || null,
              status: msg.status || "success",
              items: Array.isArray(msg.items) ? msg.items : [],
            })),
          });
          const j = await r.json().catch(() => ({}));
          const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
          if ((r.ok || r.status === 409) && tabId !== null && msg.target_id != null) {
            await chrome.storage.local.remove([`ucNativeScrapeTarget:${tabId}`]);
          }
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "browserMediaRevisitResult": {
        try {
          const r = await fetch(base + "/social/browser-revisit-result", {
            method: "POST",
            headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform,
              content_id: msg.content_id,
              claim_attempt: msg.claim_attempt ?? null,
              status: msg.status || "success",
              reason: msg.reason || null,
              observed: msg.observed ?? null,
              stored: msg.stored ?? null,
              username: msg.username || null,
            })),
          });
          const j = await r.json().catch(() => ({}));
          if (r.ok && typeof j.accepted === "boolean") {
            await clearBrowserRevisitWatchdog("browser", String(msg.platform || "").toLowerCase(), String(msg.content_id || ""), msg.claim_attempt == null ? null : Number(msg.claim_attempt));
          }
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
            headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
            body: JSON.stringify(withExtensionVersion({
              content_id: msg.content_id,
              claim_attempt: msg.claim_attempt ?? null,
              status: msg.status || "success",
              reason: msg.reason || null,
              observed: msg.observed ?? null,
              stored: msg.stored ?? null,
              username: msg.username || null,
            })),
          });
          const j = await r.json().catch(() => ({}));
          if (r.ok && typeof j.accepted === "boolean") {
            await clearBrowserRevisitWatchdog("tiktok", "tiktok", String(msg.content_id || ""), msg.claim_attempt == null ? null : Number(msg.claim_attempt));
          }
          sendResponse({ ok: r.ok, ...j });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        break;
      }
      case "ingest": {
        try {
          const platform = msg.platform || "instagram";
          const allItems = Array.isArray(msg.items) ? msg.items : [];
          const urlItems = allItems.filter((it) => !(it && it.browser_upload_only === true));
          const uploadCandidateCount = allItems.filter(shouldBrowserUploadMedia).length;
          const tiktokUploadOnlyProgress = platform === "tiktok" && allItems.length > 0 && urlItems.length === 0;
          const r = await fetch(base + "/social/ingest", {
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
            body: JSON.stringify(withExtensionVersion({
              platform,
              username: msg.username,
              collection_surface: msg.collection_surface || "unknown",
              items: urlItems,
              record_empty: !!msg.record_empty || tiktokUploadOnlyProgress,
              probe_reason: msg.probe_reason || (tiktokUploadOnlyProgress ? "browser_upload_candidates_queued" : null),
              probe_meta: msg.probe_meta || (tiktokUploadOnlyProgress ? {
                media_candidates: allItems.length,
                browser_upload_candidates: uploadCandidateCount,
              } : null),
            })),
          });
          const j = await r.json().catch(() => ({}));
          const uploadAndLog = async () => {
            const upload = await uploadBrowserMediaCandidates(base, msg, allItems);
            await log("info", `📥 ${platform} · ${msg.username} · ${j.accepted ?? urlItems.length} URL media queued, ${upload.stored}/${upload.attempted} browser-uploaded`);
            return upload;
          };
          if (platform === "tiktok" && uploadCandidateCount > 0 && !hasActiveBrowserMediaRevisit(allItems)) {
            uploadAndLog().catch((err) => log("warn", `TikTok async browser-upload failed: ${err && err.message ? err.message : err}`));
            sendResponse({
              ok: r.ok,
              upload: {
                queued: uploadCandidateCount,
                attempted: 0,
                deferred: uploadCandidateCount,
                stored: 0,
                accepted: 0,
                saved: 0,
                deduped: 0,
                async: true,
              },
            });
          } else {
            const upload = await uploadAndLog();
            sendResponse({ ok: r.ok, upload });
          }
        } catch (e) {
          await log("error", `ingest ${msg.username} failed: ${e.message}`);
          sendResponse({ ok: false, error: String(e) });
        }
        break;
      }
      case "discover": {
        try {
          const r = await fetch(base + "/social/discover", {
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
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
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
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
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
            body: JSON.stringify(withExtensionVersion({
              platform: msg.platform || "instagram", username: msg.username,
              collection_surface: msg.collection_surface || "unknown", posts: msg.posts,
            })),
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
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
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
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
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
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
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
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
            body: JSON.stringify(withExtensionVersion({ platform: msg.platform || "instagram", context: msg.context || "seen", owner: msg.owner || null, users: msg.users })),
          });
          const j = await r.json().catch(() => ({}));
          await log("info", `👤 ${msg.platform || "instagram"} · +${j.recorded ?? "?"} users (via ${msg.context || "seen"})`);
          sendResponse({ ok: r.ok });
        } catch (e) { await log("error", `users failed: ${e.message}`); sendResponse({ ok: false }); }
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
            result = await fetchJsonWithTimeout(base + `/social/strava-route-queue${query}`, 30000, await bridgeHeaders());
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
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
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
            method: "POST", headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
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
      case "tabBudget":
        sendResponse(await assertBrowserTabBudget(msg.reason || "diagnostics"));
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
        const { activeIds } = await scraperTabSelection();
        for (const p of scraperPlatforms().filter((platform) => activeIds.has(platform.id))) {
          const r = await openOrFocus(p, { active: first }); // focus the first so it's visible
          first = false;
          if (r.opened) opened++; else if (r.focused) focused++; else errors++;
        }
        await log("info", `open-scheduled: ${opened} opened, ${focused} already open${errors ? ", " + errors + " failed" : ""}`);
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
  return false;
});

async function scrapeNow() {
  await log("info", "manual 'Start/Resume loop' clicked");
  await setStatus({ cooldownUntil: 0 });
  setTimeout(() => {
    ensureLoops("manual")
      .catch((e) => log("warn", `manual loop nudge failed: ${e && e.message ? e.message : e}`));
  }, 0);
  return { ok: true, scheduled: true };
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
        const p = platformById(id);
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


