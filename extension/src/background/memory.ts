// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// memory: 12 top-level defs moved verbatim; imports added for cross-module refs.
import { scraperUrlPatterns } from "./page_recovery.js";
import { ingestBase } from "../shared/ingest_client.js";
import { log } from "../shared/log.js";
import { getStatus, setStatus } from "./status.js";
import { lastForcedReloadByTab, recordServiceWorkerRecovery, schedulePostReloadScrapeNudge } from "./tab_recovery.js";
import { _tabsOpInProgress, scraperTabSelection, selectCanonicalScraperTabRows } from "./tab_selection.js";

export const MEMORY_SENSITIVE_PLATFORMS = new Set(["facebook", "threads"]);

export const MEMORY_RELOAD_THRESHOLD_JS_MB = 250;

export const MEMORY_RELOAD_DOM_NODES = 40000;

export const MEMORY_RELOAD_MIN_INTERVAL_MIN = 90;

export const MEMORY_TIME_CAP_MIN = 240;

 // 4h safety cap on quiet days
export const MEMORY_LAST_RELOAD_KEY = "ucMemoryLastReloadByTab";

export const MEMORY_THRESHOLD_MB_OVERRIDE_KEY = "memoryReloadThresholdMB";

export const MEMORY_DOM_OVERRIDE_KEY = "memoryReloadDomNodes";

// Fetch live memory + DOM-node counts from a scraper tab via
// `chrome.scripting.executeScript`. Returns null if the tab is discarded,
// nav-blocked, or the injected read failed. `performance.memory` is a
// non-standard Chromium API but works in the content-script isolated world
// and reports the shared process heap; where the FB frame is same-process
// with the extension SW the numbers match Performance.getMetrics.
//
// Facebook (2026-08-05 verification) exposes `performance.memory` only in the
// MAIN world — the isolated world returns undefined. So when isolated-world
// injection returns a null heap, we retry in the MAIN world to catch that
// primary signal. `document.querySelectorAll('*').length` works from either
// world; MAIN-world injection is only for the memory reading.
export async function fetchTabMemoryMetrics(tabId) {
  const reader = () => ({
    js_heap_bytes:
      (typeof performance !== "undefined"
        && performance.memory
        && performance.memory.usedJSHeapSize) || null,
    js_heap_limit_bytes:
      (typeof performance !== "undefined"
        && performance.memory
        && performance.memory.jsHeapSizeLimit) || null,
    dom_nodes: document.querySelectorAll("*").length,
  });
  let isolatedResult = null;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: reader,
    });
    if (results && results[0] && results[0].result) isolatedResult = results[0].result;
  } catch (e) {
    return { error: (e && e.message) || String(e) };
  }
  if (isolatedResult && isolatedResult.js_heap_bytes !== null) return isolatedResult;
  // Fall back to MAIN world for sites that lock performance.memory behind
  // the page context (facebook.com verified 2026-08-05).
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: reader,
    });
    if (results && results[0] && results[0].result) {
      const main = results[0].result;
      // Prefer isolated dom_nodes if MAIN failed to read them (matches user's
      // scrape-cycle sampling). Prefer MAIN heap when isolated returned null.
      return {
        js_heap_bytes: main.js_heap_bytes !== null ? main.js_heap_bytes : (isolatedResult && isolatedResult.js_heap_bytes),
        js_heap_limit_bytes: main.js_heap_limit_bytes !== null ? main.js_heap_limit_bytes : (isolatedResult && isolatedResult.js_heap_limit_bytes),
        dom_nodes: main.dom_nodes !== null ? main.dom_nodes : (isolatedResult && isolatedResult.dom_nodes),
      };
    }
  } catch (e) {
    if (isolatedResult) return isolatedResult;
    return { error: (e && e.message) || String(e) };
  }
  return isolatedResult;
}

export async function memoryReloadThresholdMB() {
  // Testing override: `chrome.storage.local.set({memoryReloadThresholdMB: N})`.
  try {
    const stored = await chrome.storage.local.get(MEMORY_THRESHOLD_MB_OVERRIDE_KEY);
    const value = Number(stored[MEMORY_THRESHOLD_MB_OVERRIDE_KEY]);
    if (Number.isFinite(value) && value > 0) return value;
  } catch (e) {}
  return MEMORY_RELOAD_THRESHOLD_JS_MB;
}

export async function memoryReloadDomThreshold() {
  try {
    const stored = await chrome.storage.local.get(MEMORY_DOM_OVERRIDE_KEY);
    const value = Number(stored[MEMORY_DOM_OVERRIDE_KEY]);
    if (Number.isFinite(value) && value > 0) return value;
  } catch (e) {}
  return MEMORY_RELOAD_DOM_NODES;
}

// Threshold-driven soft reload for memory-sensitive scraper tabs (facebook,
// threads). Called from the ALARM_MEMORY alarm every MEMORY_CHECK_INTERVAL_MIN.
// Reload conditions (any of):
//   - JS heap >= threshold MB (default 250, chrome.storage override supported)
//   - DOM node count >= threshold (default 40k)
//   - time-cap: last memory reload > MEMORY_TIME_CAP_MIN ago (default 4h)
// Never reloads the same tab more often than MEMORY_RELOAD_MIN_INTERVAL_MIN.
// Reload uses chrome.tabs.reload(tabId) which preserves tab-group + pinned
// state; post-reload the standard chrome.tabs.onUpdated -> kick -> ensureLoops
// path respawns the content script, and schedulePostReloadScrapeNudge is
// scheduled as a belt for platforms with a slower warm-up.
export async function checkMemorySensitiveTabs(reason = "scheduled") {
  if (_tabsOpInProgress) return { skipped: true, reason: "tabs_operation_in_progress", checked: 0, reloaded: 0 };
  const base = await ingestBase();
  const now = Date.now();
  const jsThresholdMB = await memoryReloadThresholdMB();
  const jsThresholdBytes = jsThresholdMB * 1024 * 1024;
  const domThreshold = await memoryReloadDomThreshold();
  const minIntervalMs = MEMORY_RELOAD_MIN_INTERVAL_MIN * 60 * 1000;
  const timeCapMs = MEMORY_TIME_CAP_MIN * 60 * 1000;
  const stored = await chrome.storage.local.get(MEMORY_LAST_RELOAD_KEY);
  const lastByTab = { ...(stored[MEMORY_LAST_RELOAD_KEY] || {}) };

  let allTabs = [];
  try {
    allTabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
  } catch (e) {
    return { skipped: true, reason: "tabs_query_failed", error: (e && e.message) || String(e), checked: 0, reloaded: 0 };
  }
  const selection = selectCanonicalScraperTabRows(allTabs);
  const { activeIds } = await scraperTabSelection();
  const rows = selection.rows.filter((r) => r.platform && activeIds.has(r.platform.id) && MEMORY_SENSITIVE_PLATFORMS.has(r.platform.id));

  let checked = 0;
  let reloaded = 0;
  const reasons = [];
  for (const row of rows) {
    const tab = row.tab;
    const platform = row.platform;
    if (!tab || tab.id == null) continue;
    checked++;
    const key = `${platform.id}:${tab.id}`;
    const lastAtStored = Number(lastByTab[key] || 0);
    // Also honor lastForcedReloadByTab so we don't double-reload right after a
    // forced-cycle hard-refresh landed within the debounce window.
    const inMemLastReload = Number(lastForcedReloadByTab[String(tab.id)] || 0);
    const lastAt = Math.max(lastAtStored, inMemLastReload);
    const ageMs = lastAt ? now - lastAt : Number.POSITIVE_INFINITY;

    const metrics = await fetchTabMemoryMetrics(tab.id);
    const jsHeapBytes = metrics && Number.isFinite(Number(metrics.js_heap_bytes)) ? Number(metrics.js_heap_bytes) : null;
    const jsHeapMB = jsHeapBytes !== null ? Number((jsHeapBytes / (1024 * 1024)).toFixed(1)) : null;
    const domNodes = metrics && Number.isFinite(Number(metrics.dom_nodes)) ? Number(metrics.dom_nodes) : null;
    const metricsError = metrics && metrics.error ? String(metrics.error) : null;

    const overHeap = jsHeapBytes !== null && jsHeapBytes >= jsThresholdBytes;
    const overNodes = domNodes !== null && domNodes >= domThreshold;
    // Time-cap only fires after we have established a baseline lastAt in
    // storage — the very first check after install always writes lastAt and
    // waits for the next cycle. This avoids reloading immediately on install
    // when the tab was already open a while.
    const overTimeCap = lastAtStored > 0 && ageMs >= timeCapMs;

    let triggerReason = null;
    if (overHeap) triggerReason = "js_heap_over_threshold";
    else if (overNodes) triggerReason = "dom_nodes_over_threshold";
    else if (overTimeCap) triggerReason = "time_cap_exceeded";

    const baseExtras = {
      memory_js_heap_bytes: jsHeapBytes,
      memory_js_heap_mb: jsHeapMB,
      memory_dom_nodes: domNodes,
      memory_threshold_mb: jsThresholdMB,
      memory_dom_threshold: domThreshold,
      memory_last_reload_age_ms: Number.isFinite(ageMs) ? ageMs : null,
      memory_metrics_error: metricsError,
      memory_check_reason: reason,
    };

    if (!triggerReason) {
      await recordServiceWorkerRecovery(base, tab, platform, "memory_check_skipped", "under_threshold", baseExtras);
      if (!lastAtStored) {
        // Establish a baseline so the time-cap starts counting from the first
        // successful check rather than epoch.
        lastByTab[key] = now;
      }
      continue;
    }

    if (ageMs < minIntervalMs) {
      await recordServiceWorkerRecovery(base, tab, platform, "memory_reload_debounced", triggerReason, baseExtras);
      continue;
    }

    reasons.push(`${platform.id}:${triggerReason}`);
    await recordServiceWorkerRecovery(base, tab, platform, "memory_soft_reload", triggerReason, baseExtras);
    try {
      // bypassCache=false: use HTTP cache so the reload warm-starts faster.
      // tabs.reload preserves tab-group + pinned state (same primitive
      // refreshScraperTabs uses).
      await chrome.tabs.reload(tab.id, { bypassCache: false });
      reloaded++;
      lastByTab[key] = now;
      // Sync with the shared reload-debounce map so other reload paths
      // (forced-cycle recovery etc.) treat this as a fresh reload.
      lastForcedReloadByTab[String(tab.id)] = now;
      await log(
        "warn",
        `${platform.label}: memory soft-reload (${triggerReason}; jsHeap=${jsHeapMB !== null ? jsHeapMB + "MB" : "?"} nodes=${domNodes !== null ? domNodes : "?"})`,
      );
      schedulePostReloadScrapeNudge(base, tab, platform, `memory_soft_reload_${triggerReason}`, {
        memory_js_heap_mb: jsHeapMB,
        memory_dom_nodes: domNodes,
        memory_reload_reason: triggerReason,
      });
    } catch (e) {
      await recordServiceWorkerRecovery(base, tab, platform, "memory_reload_failed", triggerReason, {
        ...baseExtras,
        reload_error: (e && e.message) || String(e),
      });
    }
  }
  await chrome.storage.local.set({ [MEMORY_LAST_RELOAD_KEY]: lastByTab });
  const prevStatus = await getStatus();
  const prevCount = Number(prevStatus.memoryReloadCount || 0);
  const patch = { lastMemoryCheckAt: now, lastMemoryCheckReason: reason };
  if (reloaded > 0) {
    patch.memoryReloadCount = prevCount + reloaded;
    patch.lastMemoryReloadAt = now;
    patch.lastMemoryReloadReasons = reasons;
  }
  await setStatus(patch);
  return { checked, reloaded, reasons };
}
