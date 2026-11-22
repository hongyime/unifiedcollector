// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// tab_recovery: 35 top-level defs moved verbatim; imports added for cross-module refs.
import { SCRAPER_HEARTBEAT_TIMEOUT_MS, extensionVersion, withExtensionVersion } from "./heartbeat.js";
import { platformScrapeDisabled } from "./scrape_config.js";
import { postJsonWithTimeout } from "../shared/ingest_client.js";
import { log } from "../shared/log.js";

export const HOME_NAV_HARD_REFRESH_PLATFORMS = new Set(["x", "threads", "lemon8"]);

export const lastForcedCycleByTab = {};

export const lastForcedReloadByTab = {};

export const lastForcedFailureByTab = {};

export const FORCED_CYCLE_DEBOUNCE_MS = 5 * 60 * 1000;

export const FORCED_CYCLE_HARD_RELOAD_MS_BY_PLATFORM = {
  x: 5 * 60 * 1000,
  facebook: 7 * 60 * 1000,
  tiktok: 6 * 60 * 1000,
  lemon8: 6 * 60 * 1000,
  threads: 6 * 60 * 1000,
  strava: 7 * 60 * 1000,
  instagram: 12 * 60 * 1000,
};

export const FORCED_CYCLE_RELOAD_DEBOUNCE_MS = 4 * 60 * 1000;

export const FORCED_CYCLE_FAILURE_DEBOUNCE_MS = 90 * 1000;

export const TAB_MESSAGE_TIMEOUT_MS = 30000;

export const MESSAGE_TIMEOUT_STALE_RELOAD_SECONDS_BY_PLATFORM = {
  facebook: 10 * 60,
  instagram: 10 * 60,
  lemon8: 10 * 60,
  x: 10 * 60,
  tiktok: 10 * 60,
  threads: 10 * 60,
};

export const EXTENSION_AUTO_RELOAD_STATE_KEY = "ucExtensionAutoReloadState";

export const EXTENSION_AUTO_RELOAD_DEBOUNCE_MS = 30 * 1000;

export const EXTENSION_AUTO_RELOAD_DELAY_MS = 1200;

export const EXTENSION_AUTO_RELOAD_MAX_ATTEMPTS = 8;

export const EXTENSION_AUTO_RELOAD_ALARM = "uc-extension-auto-reload";

export function forcedCycleHardReloadMs(platformId) {
  return FORCED_CYCLE_HARD_RELOAD_MS_BY_PLATFORM[platformId] || 5 * 60 * 1000;
}

export function hardRefreshNavigationUrl(platform, currentUrl, now) {
  if (!platform || !HOME_NAV_HARD_REFRESH_PLATFORMS.has(platform.id)) return null;
  const current = String(currentUrl || "");
  if (platform.id === "x") {
    return "https://x.com/home";
  }
  if (platform.id === "threads") {
    return `https://www.threads.com/?uc_recover=${Math.floor(now / 1000)}`;
  }
  return platform.url && current !== platform.url ? platform.url : null;
}

export function isNoReceiverError(err) {
  return /Could not establish connection|Receiving end does not exist|Extension context invalidated/i.test(
    String((err && err.message) || err || "")
  );
}

export function isTabMessageTimeout(err) {
  return /tab message timed out/i.test(String((err && err.message) || err || ""));
}

export function messageTimeoutStaleReloadSeconds(platformId) {
  return MESSAGE_TIMEOUT_STALE_RELOAD_SECONDS_BY_PLATFORM[platformId] || 10 * 60;
}

export async function sendTabMessageWithTimeout(tabId, message, timeoutMs = TAB_MESSAGE_TIMEOUT_MS) {
  let timer = null;
  try {
    return await Promise.race([
      chrome.tabs.sendMessage(tabId, message),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("tab message timed out")), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export async function recordServiceWorkerRecovery(base, tab, platform, status, reason, extra = {}) {
  if (!base || !platform || !platform.id) return;
  try {
    await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
      platform: platform.id,
      label: platform.label || platform.id,
      running: true,
      tab_id: tab && tab.id != null ? tab.id : null,
      url: tab && tab.url ? tab.url : null,
      page_title: tab && tab.title ? tab.title : null,
      health_status: status,
      health_reason: reason || null,
      service_worker_recovery: true,
      ...extra,
    }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
  } catch (e) {}
}

export async function refreshContentScriptAndNudge(base, tab, platform, reason, extra = {}) {
  if (!tab || tab.id == null || !platform || !platform.id) return false;
  let freshTab = tab;
  try {
    freshTab = await chrome.tabs.get(tab.id);
  } catch (e) {}
  const refreshed = await hardRefreshForcedCycleTab(base, freshTab, platform, reason || "content_script_receiver_missing", {
    ...extra,
    recovery: "manifest_content_script_refresh",
  });
  await recordServiceWorkerRecovery(base, freshTab, platform, "content_script_manifest_refresh_scheduled", reason || "content_script_receiver_missing", {
    ...extra,
    refresh_ok: refreshed || null,
  });
  return refreshed;
}

export const POST_RELOAD_NUDGE_DELAY_MS_BY_PLATFORM = {
  facebook: 30000,
  instagram: 25000,
  lemon8: 25000,
  threads: 45000,
  tiktok: 75000,
  x: 75000,
};

export const POST_RELOAD_NUDGE_RETRY_DELAY_MS_BY_PLATFORM = {
  threads: 60000,
  tiktok: 90000,
  x: 90000,
};

export function postReloadScrapeNudgeDelayMs(platform, extra = {}) {
  if (Number.isFinite(Number(extra.post_reload_delay_ms)) && Number(extra.post_reload_delay_ms) > 0) {
    return Number(extra.post_reload_delay_ms);
  }
  return POST_RELOAD_NUDGE_DELAY_MS_BY_PLATFORM[platform && platform.id] || 12000;
}

export function postReloadScrapeNudgeRetryDelayMs(platform) {
  return POST_RELOAD_NUDGE_RETRY_DELAY_MS_BY_PLATFORM[platform && platform.id] || 45000;
}

export function schedulePostReloadScrapeNudge(base, tab, platform, reason, extra = {}) {
  if (!tab || tab.id == null || !platform || !platform.id) return;
  const tabId = tab.id;
  const delayMs = postReloadScrapeNudgeDelayMs(platform, extra);
  setTimeout(() => {
    (async () => {
      let freshTab = tab;
      try {
        freshTab = await chrome.tabs.get(tabId);
      } catch (e) {}
      try {
        await sendTabMessageWithTimeout(tabId, {
          type: "scrapeCycle",
          reason: reason || "post_reload_recovery",
        });
        await recordServiceWorkerRecovery(base, freshTab, platform, "post_reload_scrape_nudge_sent", reason || "post_reload_recovery", {
          ...extra,
          post_reload_delay_ms: delayMs,
        });
      } catch (e) {
        const messageTimedOut = isTabMessageTimeout(e);
        if (isNoReceiverError(e)) {
          const refreshed = await refreshContentScriptAndNudge(base, freshTab, platform, reason || "post_reload_recovery", {
            ...extra,
            post_reload_delay_ms: delayMs,
            recovery: "post_reload_manifest_refresh",
          });
          if (refreshed) return;
        }
        if (messageTimedOut && !extra.post_reload_retry) {
          const retryDelayMs = postReloadScrapeNudgeRetryDelayMs(platform);
          await recordServiceWorkerRecovery(base, freshTab, platform, "post_reload_scrape_nudge_retry_scheduled", reason || "post_reload_recovery", {
            ...extra,
            cycle_error: e && e.message ? e.message : String(e),
            message_timeout: true,
            post_reload_delay_ms: delayMs,
            post_reload_retry_delay_ms: retryDelayMs,
          });
          schedulePostReloadScrapeNudge(base, freshTab, platform, reason || "post_reload_recovery", {
            ...extra,
            post_reload_retry: true,
            post_reload_delay_ms: retryDelayMs,
          });
          return;
        }
        if (messageTimedOut) {
          const refreshed = await refreshContentScriptAndNudge(base, freshTab, platform, reason || "post_reload_recovery", {
            ...extra,
            cycle_error: e && e.message ? e.message : String(e),
            message_timeout: true,
            post_reload_delay_ms: delayMs,
            recovery: "post_reload_timeout_manifest_refresh",
          });
          if (refreshed) return;
        }
        await recordServiceWorkerRecovery(base, freshTab, platform, "post_reload_scrape_nudge_failed", reason || "post_reload_recovery", {
          ...extra,
          cycle_error: e && e.message ? e.message : String(e),
          message_timeout: messageTimedOut || null,
          post_reload_delay_ms: delayMs,
        });
      }
    })().catch(() => {});
  }, delayMs);
}

export async function hardRefreshForcedCycleTab(base, tab, platform, reason, extra = {}) {
  if (!tab || tab.id == null || !platform || !platform.id) return false;
  const now = Date.now();
  const key = String(tab.id);
  const lastReloadAt = Number(lastForcedReloadByTab[key] || 0);
  if (lastReloadAt && now - lastReloadAt < FORCED_CYCLE_RELOAD_DEBOUNCE_MS) {
    await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_reload_skipped", "reload_debounce", {
      reload_age_ms: now - lastReloadAt,
      ...extra,
    });
    return false;
  }
  const currentUrl = tab && tab.url ? String(tab.url) : "";
  const targetUrl = hardRefreshNavigationUrl(platform, currentUrl, now);
  await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_hard_refresh", reason || "forced_cycle_recovery", {
    ...extra,
    recovery_nav: targetUrl ? "home_url" : "reload",
    recovery_target_url: targetUrl || null,
  });
  if (targetUrl) {
    await chrome.tabs.update(tab.id, { url: targetUrl });
  } else {
    await chrome.tabs.reload(tab.id, { bypassCache: true });
  }
  lastForcedReloadByTab[key] = now;
  lastForcedCycleByTab[key] = 0;
  schedulePostReloadScrapeNudge(base, tab, platform, reason || "forced_cycle_recovery", extra);
  return true;
}

export async function refreshTabForMissingContentScript(base, tab, platform, reason, extra = {}) {
  if (!tab || tab.id == null || !platform || !platform.id) return false;
  // Reload the page so Chrome runs the manifest content script in a clean page
  // context. Re-injecting content.js into an already loaded page can redeclare
  // top-level consts in the isolated world and break scraping.
  return refreshContentScriptAndNudge(base, tab, platform, reason || "content_script_receiver_missing", {
    recovery: "manifest_content_script_refresh",
    ...extra,
  });
}

export function parseHeartbeatResponseBody(responseBody) {
  if (!responseBody) return null;
  if (typeof responseBody === "object") return responseBody;
  try { return JSON.parse(String(responseBody)); } catch (e) { return null; }
}

export function normalizeExtensionVersion(value) {
  return String(value || "").trim().replace(/^v/i, "");
}

export async function maybeAutoReloadExtension(base, tab, platform, body, reason) {
  if (!body || body.reload_extension !== true) return false;
  const expected = normalizeExtensionVersion(body.expected_extension_version);
  const current = normalizeExtensionVersion(extensionVersion());
  if (!expected || !current || expected === current) return false;

  const now = Date.now();
  const stored = await chrome.storage.local.get(EXTENSION_AUTO_RELOAD_STATE_KEY);
  const state = stored[EXTENSION_AUTO_RELOAD_STATE_KEY] || {};
  const attempts = state.expected === expected ? Number(state.attempts || 0) : 0;
  const lastAt = state.expected === expected ? Number(state.last_at || 0) : 0;
  const reloadReason = body.reload_reason || reason || "extension_version_mismatch";

  if (attempts >= EXTENSION_AUTO_RELOAD_MAX_ATTEMPTS) {
    await recordServiceWorkerRecovery(base, tab, platform, "extension_auto_reload_gave_up", reloadReason, {
      expected_extension_version: expected,
      current_extension_version: current,
      reload_attempts: attempts,
      reload_max_attempts: EXTENSION_AUTO_RELOAD_MAX_ATTEMPTS,
    });
    return false;
  }
  if (lastAt && now - lastAt < EXTENSION_AUTO_RELOAD_DEBOUNCE_MS) {
    await recordServiceWorkerRecovery(base, tab, platform, "extension_auto_reload_debounced", reloadReason, {
      expected_extension_version: expected,
      current_extension_version: current,
      reload_attempts: attempts,
      reload_age_ms: now - lastAt,
    });
    return true;
  }

  const nextAttempts = attempts + 1;
  await chrome.storage.local.set({
    [EXTENSION_AUTO_RELOAD_STATE_KEY]: {
      expected,
      attempts: nextAttempts,
      last_at: now,
    },
  });
  await recordServiceWorkerRecovery(base, tab, platform, "extension_auto_reload_scheduled", reloadReason, {
    expected_extension_version: expected,
    current_extension_version: current,
    reload_attempt: nextAttempts,
    reload_delay_ms: EXTENSION_AUTO_RELOAD_DELAY_MS,
  });
  await log("warn", `extension ${current} is older than expected ${expected}; reloading extension`);
  try {
    await chrome.alarms.create(EXTENSION_AUTO_RELOAD_ALARM, { when: Date.now() + EXTENSION_AUTO_RELOAD_DELAY_MS });
  } catch (e) {}
  try { chrome.runtime.reload(); } catch (e) {}
  return true;
}

async function _handleStaleForcedCycle(base, tab, platform, body, key, now, lastForcedAt, forcedAgeMs, hardReloadMs) {
  if (!lastForcedAt || forcedAgeMs <= hardReloadMs) return false;
  const lastReloadAt = Number(lastForcedReloadByTab[key] || 0);
  if (!lastReloadAt || now - lastReloadAt > FORCED_CYCLE_RELOAD_DEBOUNCE_MS) {
    try {
      await hardRefreshForcedCycleTab(base, tab, platform, "stale_forced_cycle", {
        forced_age_ms: forcedAgeMs,
        hard_reload_ms: hardReloadMs,
        content_age_seconds: body.content_age_seconds || null,
      });
      await log("warn", `${platform.label}: stale forced scrape did not finish after ${Math.round(forcedAgeMs / 1000)}s; hard-refreshed tab`);
    } catch (e) {
      await log("warn", `${platform.label}: stale forced scrape hard-refresh failed: ${e && e.message ? e.message : e}`);
    }
    return true;
  }
  return false;
}

async function _handleForcedCycleFailure(base, tab, platform, body, key, firstErr) {
  const cycleError = firstErr && firstErr.message ? firstErr.message : String(firstErr);
  const failKey = `${key}:${platform && platform.id ? platform.id : "unknown"}`;
  const lastFailureAt = Number(lastForcedFailureByTab[failKey] || 0);
  const failureAgeMs = lastFailureAt ? Date.now() - lastFailureAt : 0;
  const receiverMissing = isNoReceiverError(firstErr);
  const messageTimedOut = isTabMessageTimeout(firstErr);
  if (receiverMissing && lastFailureAt && failureAgeMs < FORCED_CYCLE_FAILURE_DEBOUNCE_MS) {
    await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_debounced", body.force_reason || "browser_content_stale", {
      content_age_seconds: body.content_age_seconds || null,
      cycle_error: cycleError,
      failure_age_ms: failureAgeMs,
      no_receiver: true,
    });
    return;
  }
  lastForcedFailureByTab[failKey] = Date.now();
  if (messageTimedOut) {
    const contentAgeSeconds = Number(body.content_age_seconds || 0);
    const staleReloadSeconds = messageTimeoutStaleReloadSeconds(platform && platform.id);
    if (Number.isFinite(contentAgeSeconds) && contentAgeSeconds >= staleReloadSeconds) {
      const reloaded = await hardRefreshForcedCycleTab(base, tab, platform, "message_timeout_content_stale", {
        content_age_seconds: contentAgeSeconds,
        stale_reload_seconds: staleReloadSeconds,
        cycle_error: cycleError,
        message_timeout: true,
        recovery: "message_timeout_stale_refresh",
      });
      await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_timed_out", body.force_reason || "browser_content_stale", {
        content_age_seconds: contentAgeSeconds,
        stale_reload_seconds: staleReloadSeconds,
        cycle_error: cycleError,
        message_timeout: true,
        stale_refresh_attempted: true,
        stale_refresh_ok: reloaded || null,
      });
      if (!reloaded) {
        const refreshed = await refreshContentScriptAndNudge(base, tab, platform, "forced_cycle_reload_debounced_timeout", {
          content_age_seconds: contentAgeSeconds,
          stale_reload_seconds: staleReloadSeconds,
          cycle_error: cycleError,
          message_timeout: true,
          recovery: "reload_debounced_manifest_refresh",
        });
        await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_reload_debounced_refresh", body.force_reason || "browser_content_stale", {
          content_age_seconds: contentAgeSeconds,
          stale_reload_seconds: staleReloadSeconds,
          cycle_error: cycleError,
          message_timeout: true,
          refresh_attempted: true,
          refresh_ok: refreshed || null,
        });
      }
      await log("warn", `${platform.label}: stale-content forced scrape message timed out; ${reloaded ? "hard-refreshed stale tab" : "refresh skipped by debounce"}`);
      return;
    }
    const refreshed = await refreshContentScriptAndNudge(base, tab, platform, "forced_cycle_message_timeout", {
      content_age_seconds: body.content_age_seconds || null,
      cycle_error: cycleError,
      message_timeout: true,
      recovery: "message_timeout_manifest_refresh",
    });
    await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_timed_out", body.force_reason || "browser_content_stale", {
      content_age_seconds: body.content_age_seconds || null,
      cycle_error: cycleError,
      message_timeout: true,
      refresh_attempted: true,
      refresh_ok: refreshed || null,
    });
    await log("warn", `${platform.label}: stale-content forced scrape message timed out; ${refreshed ? "hard-refreshed tab for manifest content script" : "leaving the tab running and retrying later"}`);
    return;
  }
  await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_failed", body.force_reason || "browser_content_stale", {
    content_age_seconds: body.content_age_seconds || null,
    cycle_error: cycleError,
    no_receiver: receiverMissing || null,
  });
  try {
    if (receiverMissing) {
      try {
        const reloaded = await refreshTabForMissingContentScript(base, tab, platform, "content_script_receiver_missing", {
          content_age_seconds: body.content_age_seconds || null,
          cycle_error: cycleError,
          no_receiver: true,
        });
        if (reloaded) {
          await log("warn", `${platform.label}: content script receiver missing; hard-refreshed tab so manifest content.js can reload`);
          return;
        }
      } catch (refreshErr) {
        await recordServiceWorkerRecovery(base, tab, platform, "content_script_refresh_failed", "content_script_receiver_missing", {
          content_age_seconds: body.content_age_seconds || null,
          cycle_error: cycleError,
          refresh_error: refreshErr && refreshErr.message ? refreshErr.message : String(refreshErr),
          no_receiver: true,
        });
      }
    }
    const reloaded = await hardRefreshForcedCycleTab(base, tab, platform, receiverMissing ? "content_script_receiver_missing" : "failed_force_message", {
      content_age_seconds: body.content_age_seconds || null,
      cycle_error: cycleError,
      no_receiver: receiverMissing || null,
    });
    if (reloaded) {
      await log("warn", `${platform.label}: stale-content forced scrape failed; hard-refreshed tab (${cycleError})`);
    } else {
      await log("warn", `${platform.label}: stale-content forced scrape failed; reload already debounced (${cycleError})`);
    }
  } catch (reloadErr) {
    await log("warn", `${platform.label}: stale-content forced scrape failed and hard-refresh failed: ${reloadErr && reloadErr.message ? reloadErr.message : reloadErr}`);
  }
}

export async function maybeForceScrapeCycle(tab, platform, responseBody, reason, base = null) {
  const body = parseHeartbeatResponseBody(responseBody);
  if (!body) return;
  if (await platformScrapeDisabled(platform)) {
    return;
  }
  const reloadScheduled = await maybeAutoReloadExtension(base, tab, platform, body, reason);
  if (reloadScheduled || body.force_cycle !== true || !tab || !tab.id) return;
  const now = Date.now();
  const key = String(tab.id);
  const lastForcedAt = Number(lastForcedCycleByTab[key] || 0);
  const forcedAgeMs = lastForcedAt ? now - lastForcedAt : 0;
  const hardReloadMs = forcedCycleHardReloadMs(platform && platform.id);
  if (await _handleStaleForcedCycle(base, tab, platform, body, key, now, lastForcedAt, forcedAgeMs, hardReloadMs)) return;
  if (lastForcedAt && forcedAgeMs < FORCED_CYCLE_DEBOUNCE_MS) return;
  const recoveryMessage = {
    type: "scrapeCycle",
    reason: body.force_reason || "browser_content_stale",
    content_age_seconds: body.content_age_seconds || null,
  };
  try {
    lastForcedCycleByTab[key] = now;
    await sendTabMessageWithTimeout(tab.id, recoveryMessage);
    await recordServiceWorkerRecovery(base, tab, platform, "forced_cycle_request_sent", body.force_reason || "browser_content_stale", {
      content_age_seconds: body.content_age_seconds || null,
      message_type: recoveryMessage.type,
    });
    await log("warn", `${platform.label}: nudged scraper loop because browser content is stale`);
  } catch (firstErr) {
    await _handleForcedCycleFailure(base, tab, platform, body, key, firstErr);
  }
}


export async function scheduleMaybeForceScrapeCycle(tab, platform, responseBody, reason, base = null) {
  try {
    await maybeForceScrapeCycle(tab, platform, responseBody, reason, base);
  } catch (e) {
    await log("warn", `${platform && platform.label ? platform.label : "scraper"} recovery failed: ${e && e.message ? e.message : e}`);
  }
}
