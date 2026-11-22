// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// startup: 10 top-level defs moved verbatim; imports added for cross-module refs.
import { reportBridgeHeartbeat, reportScraperTabHeartbeats } from "./heartbeat.js";
import { ensureLoops } from "./loops.js";
import { clearPageRecoveryState, shouldResetPageRecoveryForStartup } from "./page_recovery.js";
import { log } from "../shared/log.js";
import { setStatus } from "./status.js";
import { ensureScraperTabsOpen, refreshScraperTabs } from "./tab_selection.js";

export const ALARM = "uc-scrape";

// DEFAULT_INGEST / DEFAULT_CONTROL / ingestBase() / controlBase() /
// postJsonWithTimeout() live in src/shared/ingest_client.js — imported at
// the top of this file.
// LOG_KEY / LOG_MAX / log(level, msg) live in src/shared/log.js and are
// imported at the top of this file.
export const WATCHDOG_MIN = 7;

// ---- watchdog + AUTO-TABS (open + refresh) --------------------------------
// The 83h-stall problem: a closed/orphaned tab = no scraping. So the worker now
// auto-OPENS every scraper tab (background, unpinned by default) and auto-REFRESHES them
// hourly — reloading respawns the content script + loop AND pulls fresh content,
// so it can never silently die again.
export const ALARM_REFRESH = "uc-refresh";

export const REFRESH_MIN = 45;

// ---- memory-driven soft reload -------------------------------------------
// Facebook (and threads to a lesser extent) accumulate DOM nodes from infinite
// scroll — baseline 2026-08-05 showed the FB tab at 260MB JS heap / 18k DOM
// elements / 32k Blink nodes / 27k LayoutObjects after just a few hours; the
// tab's untouched growth path leads to Chrome killing it OOM. Every kill costs
// re-login on an unlucky cookie flush and leaves the tab group short a member.
// The 45-min ALARM_REFRESH cycle already reloads all scraper tabs, but its
// cadence lets FB heap grow well past 250MB between reloads on heavy-feed days
// (staggered reloads mean each tab may wait 90-120s past nominal cadence too).
// We add a THIRD alarm that polls memory-sensitive tabs and soft-reloads once
// the JS heap or DOM-node count crosses a threshold, or once the tab has run
// for MEMORY_TIME_CAP_MIN without a memory-driven reload. `chrome.tabs.reload`
// preserves tab-group membership + pinned state (same primitive
// refreshScraperTabs uses), so this is safe to layer on top of the existing
// refresh loop.
export const ALARM_MEMORY = "uc-memory-check";

export const MEMORY_CHECK_INTERVAL_MIN = 30;

 // guard against overlapping open/refresh runs (no spam)
export let _startupRecoveryChain = Promise.resolve();

export async function scheduleAlarm() {
  chrome.alarms.create(ALARM, { periodInMinutes: WATCHDOG_MIN });
  chrome.alarms.create(ALARM_REFRESH, { periodInMinutes: REFRESH_MIN });
  chrome.alarms.create(ALARM_MEMORY, { periodInMinutes: MEMORY_CHECK_INTERVAL_MIN });
  await setStatus({ swStartedAt: Date.now() });
  const ver = (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || "?";
  await log("info", `worker started v${ver} - jittered tabs + ${WATCHDOG_MIN}-min watchdog + ${REFRESH_MIN}-min refresh + ${MEMORY_CHECK_INTERVAL_MIN}-min memory check`);
  await reportBridgeHeartbeat("schedule_alarm");
  await reportScraperTabHeartbeats("schedule_alarm");
}

export function runStartupRecovery(reason, options = {}) {
  const attempt = Number(options.attempt || 0);
  _startupRecoveryChain = _startupRecoveryChain
    .catch(() => null)
    .then(() => performStartupRecovery(reason, { ...options, attempt }))
    .catch((e) => log("warn", `${reason} recovery failed: ${e && e.message ? e.message : e}`));
  return _startupRecoveryChain;
}

export async function performStartupRecovery(reason, options = {}) {
  const attempt = Number(options.attempt || 0);
  const retries = Math.max(0, Number(options.retries || 0));
  const force = !!options.force;
  const refreshTabs = !!options.refreshTabs;
  const openTabs = options.openTabs !== false;
  const refreshForce = options.refreshForce !== false;
  await setStatus({
    swStartedAt: Date.now(),
    lastStartupRecoveryAt: Date.now(),
    lastStartupRecoveryReason: reason,
    lastStartupRecoveryAttempt: attempt,
  });
  await log("info", `startup recovery ${reason}${attempt ? " retry " + attempt : ""}`);
  if (attempt === 0 && shouldResetPageRecoveryForStartup(reason)) {
    await clearPageRecoveryState(reason).catch(() => {});
  }
  await scheduleAlarm().catch((e) => log("warn", `${reason} schedule failed: ${e && e.message ? e.message : e}`));
  if (openTabs) {
    await ensureScraperTabsOpen(reason, { force })
      .catch((e) => log("warn", `${reason} tab audit failed: ${e && e.message ? e.message : e}`));
  }
  await reportBridgeHeartbeat(reason)
    .catch((e) => log("warn", `${reason} bridge heartbeat failed: ${e && e.message ? e.message : e}`));
  await reportScraperTabHeartbeats(reason)
    .catch((e) => log("warn", `${reason} scraper heartbeat failed: ${e && e.message ? e.message : e}`));
  if (refreshTabs) {
    const refreshGapMs = Number.isFinite(Number(options.refreshGapMs)) ? Number(options.refreshGapMs) : 1500;
    await refreshScraperTabs({ bypassCache: true, force: refreshForce || force, reason, gapMs: refreshGapMs })
      .catch((e) => log("warn", `${reason} tab refresh failed: ${e && e.message ? e.message : e}`));
    await reportScraperTabHeartbeats(reason + "_refresh")
      .catch((e) => log("warn", `${reason} refresh heartbeat failed: ${e && e.message ? e.message : e}`));
  }
  await ensureLoops(reason)
    .catch((e) => log("warn", `${reason} loop nudge failed: ${e && e.message ? e.message : e}`));

  if (attempt < retries) {
    const delay = [45000, 150000, 360000][Math.min(attempt, 2)];
    setTimeout(() => {
      runStartupRecovery(reason, { ...options, attempt: attempt + 1, retries });
    }, delay);
  }
}
