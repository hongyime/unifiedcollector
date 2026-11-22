// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// heartbeat: 9 top-level defs moved verbatim; imports added for cross-module refs.
import { platformHosts, scraperPlatforms, scraperUrlPatterns } from "./page_recovery.js";
import { ingestBase, postJsonWithTimeout } from "../shared/ingest_client.js";
import { log } from "../shared/log.js";
import { setStatus } from "./status.js";
import { maybeAutoReloadExtension, parseHeartbeatResponseBody, scheduleMaybeForceScrapeCycle } from "./tab_recovery.js";
import { scraperTabSelection, selectCanonicalScraperTabRows } from "./tab_selection.js";
import { mapLimit } from "./util.js";

export const SCRAPER_HEARTBEAT_TIMEOUT_MS = 12000;

export const SCRAPER_HEARTBEAT_CONCURRENCY = 3;

export function extensionVersion() {
  return (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || null;
}

export function withExtensionVersion(payload) {
  return { ...payload, extension_version: extensionVersion() };
}

// postJsonWithTimeout(url, payload, timeoutMs) lives in
// src/shared/ingest_client.js and is imported at the top of this file.
export async function reportBridgeHeartbeat(reason) {
  const base = await ingestBase();
  const payload = withExtensionVersion({
    platform: "bridge",
    label: "UnifiedCollector Bridge",
    running: true,
    tab_id: "service_worker",
    url: "chrome-extension://" + chrome.runtime.id + "/background.js",
    health_status: "service_worker_active",
    health_reason: reason || "startup",
  });
  try {
    const result = await postJsonWithTimeout(base + "/social/browser-heartbeat", payload);
    const body = parseHeartbeatResponseBody(result && result.body);
    await maybeAutoReloadExtension(
      base,
      null,
      { id: "bridge", label: "UnifiedCollector Bridge" },
      body,
      reason || "bridge_heartbeat",
    );
    await setStatus({
      lastBridgeHeartbeatOkAt: Date.now(),
      lastBridgeHeartbeatError: null,
      lastBridgeHeartbeatBase: base,
      lastBridgeHeartbeatReason: reason || "startup",
    });
  } catch (e) {
    const err = String(e && e.message ? e.message : e);
    await setStatus({
      lastBridgeHeartbeatFailAt: Date.now(),
      lastBridgeHeartbeatError: err,
      lastBridgeHeartbeatBase: base,
      lastBridgeHeartbeatReason: reason || "startup",
    });
    await log("warn", `browser heartbeat failed to reach ${base}: ${err}`);
  }
}

export async function sendManualIngestProbe() {
  const base = await ingestBase();
  const payload = withExtensionVersion({
    platform: "bridge",
    label: "UnifiedCollector Bridge",
    running: true,
    tab_id: "manual_test",
    url: "chrome-extension://" + chrome.runtime.id + "/tabs.html",
    health_status: "manual_ingest_probe",
    health_reason: "tabs_page_test",
  });
  try {
    const { response: r } = await postJsonWithTimeout(base + "/social/browser-heartbeat", payload, 6000);
    await setStatus({ lastManualIngestProbeAt: Date.now(), lastManualIngestProbeOk: true, lastManualIngestProbeError: null });
    await log("info", `manual ingest probe ok (${base})`);
    return { ok: true, status: r.status, base };
  } catch (e) {
    const err = String(e && e.message ? e.message : e);
    await setStatus({ lastManualIngestProbeAt: Date.now(), lastManualIngestProbeOk: false, lastManualIngestProbeError: err });
    await log("error", `manual ingest probe failed: ${err}`);
    return { ok: false, error: err, base };
  }
}

export function platformForTabUrl(url) {
  let host = "";
  try { host = new URL(url || "").host; } catch (e) { return null; }
  return scraperPlatforms().find((p) => platformHosts(p).includes(host)) || null;
}

export async function reportScraperTabHeartbeats(reason) {
  const base = await ingestBase();
  let seen = 0;
  let sent = 0;
  let failed = 0;
  let lastError = null;
  let canonical = 0;
  let skipped = 0;
  try {
    const allTabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
    const selection = selectCanonicalScraperTabRows(allTabs);
    const activeIds = (await scraperTabSelection()).activeIds;
    const tabs = selection.rows.filter((row) => activeIds.has(row.platform.id));
    seen = selection.seen;
    canonical = tabs.length;
    skipped = selection.skipped;
    await mapLimit(tabs, SCRAPER_HEARTBEAT_CONCURRENCY, async ({ tab, platform }) => {
      try {
        const result = await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
          platform: platform.id,
          label: platform.label,
          running: true,
          tab_id: tab.id,
          url: tab.url || null,
          health_status: "background_tab_seen",
          health_reason: reason || "background_watchdog",
          page_title: tab.title || null,
        }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
        await scheduleMaybeForceScrapeCycle(tab, platform, result && result.body, reason || "background_watchdog", base);
        sent++;
      } catch (e) {
        failed++;
        lastError = String(e && e.message ? e.message : e);
      }
    });
  } catch (e) {
    lastError = String(e && e.message ? e.message : e);
  }
  await setStatus({
    lastScraperHeartbeatAt: Date.now(),
    lastScraperHeartbeatSeen: seen,
    lastScraperHeartbeatSent: sent,
    lastScraperHeartbeatFailed: failed,
    lastScraperHeartbeatCanonical: canonical,
    lastScraperHeartbeatSkippedDuplicates: skipped,
    lastScraperHeartbeatError: lastError,
    lastScraperHeartbeatBase: base,
    lastScraperHeartbeatReason: reason || "background_watchdog",
  });
  if (failed || lastError) {
    await log("warn", `scraper heartbeat delivery: ${sent}/${seen} sent to ${base}${lastError ? " (" + lastError + ")" : ""}`);
  }
  await reportScraperHeartbeatSummary(base, reason, { seen, sent, failed, canonical, skipped, lastError })
    .catch(() => {});
}

export async function reportScraperHeartbeatSummary(base, reason, summary) {
  const seen = Number(summary && summary.seen || 0);
  const sent = Number(summary && summary.sent || 0);
  const failed = Number(summary && summary.failed || 0);
  const lastError = summary && summary.lastError ? String(summary.lastError).slice(0, 240) : null;
  const healthStatus = seen <= 0
    ? "scraper_tabs_missing"
    : failed > 0 || sent <= 0
      ? "scraper_heartbeat_degraded"
      : "scraper_heartbeat_ok";
  try {
    await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
      platform: "bridge",
      label: "UnifiedCollector Bridge",
      running: true,
      tab_id: "scraper_tabs",
      url: "chrome-extension://" + chrome.runtime.id + "/background.js",
      health_status: healthStatus,
      health_reason: reason || "background_watchdog",
      scraper_tabs_seen: seen,
      scraper_tabs_sent: sent,
      scraper_tabs_failed: failed,
      scraper_tabs_canonical: Number(summary && summary.canonical || 0),
      scraper_tabs_skipped: Number(summary && summary.skipped || 0),
      scraper_heartbeat_error: lastError,
    }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
    await setStatus({
      lastBridgeHeartbeatOkAt: Date.now(),
      lastBridgeHeartbeatError: null,
      lastBridgeHeartbeatBase: base,
      lastBridgeHeartbeatReason: reason || "scraper_heartbeat_summary",
    });
  } catch (e) {
    await setStatus({
      lastBridgeHeartbeatFailAt: Date.now(),
      lastBridgeHeartbeatError: String(e && e.message ? e.message : e),
      lastBridgeHeartbeatBase: base,
      lastBridgeHeartbeatReason: reason || "scraper_heartbeat_summary",
    });
    throw e;
  }
}
