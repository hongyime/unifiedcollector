// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// revisit_watchdog: 9 top-level defs moved verbatim; imports added for cross-module refs.
import { SCRAPER_HEARTBEAT_TIMEOUT_MS, withExtensionVersion } from "./heartbeat.js";
import { ingestBase, postJsonWithTimeout } from "../shared/ingest_client.js";
import { log } from "../shared/log.js";

export const BROWSER_REVISIT_WATCHDOG_PREFIX = "uc-browser-revisit-watchdog:";

export const BROWSER_REVISIT_NAVIGATION_TIMEOUT_MS = 120000;

export const BROWSER_REVISIT_RESULT_TIMEOUT_MS = 10 * 60 * 1000;

export function browserRevisitWatchdogKey(queue, platform) {
  return `ucBrowserRevisitWatchdog:${queue}:${platform}`;
}

export function browserRevisitWatchdogAlarm(queue, platform) {
  return `${BROWSER_REVISIT_WATCHDOG_PREFIX}${queue}:${platform}`;
}

export async function armBrowserRevisitWatchdog(msg, sender) {
  const queue = msg && msg.queue === "tiktok" ? "tiktok" : "browser";
  const platform = queue === "tiktok" ? "tiktok" : String(msg && msg.platform || "").toLowerCase();
  const contentId = String(msg && msg.content_id || "").trim();
  const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
  if (!platform || !contentId || tabId == null) return { ok: false };
  const key = browserRevisitWatchdogKey(queue, platform);
  const alarmName = browserRevisitWatchdogAlarm(queue, platform);
  const values = await chrome.storage.local.get(key);
  const existing = values && values[key];
  const claimAttempt = msg.claim_attempt == null ? null : Number(msg.claim_attempt);
  if (
    existing && existing.content_id === contentId && existing.claim_attempt === claimAttempt &&
    existing.tab_id === tabId && existing.url === msg.url
  ) {
    await chrome.alarms.create(alarmName, { when: existing.deadline_at || Date.now() + BROWSER_REVISIT_NAVIGATION_TIMEOUT_MS });
    return { ok: true };
  }
  const startedAt = Number(msg.navigation_started_at || Date.now());
  const state = {
    queue,
    platform,
    content_id: contentId,
    claim_attempt: claimAttempt,
    tab_id: tabId,
    url: String(msg.url || ""),
    phase: "navigation",
    deadline_at: startedAt + BROWSER_REVISIT_NAVIGATION_TIMEOUT_MS,
  };
  await chrome.storage.local.set({ [key]: state });
  await chrome.alarms.create(alarmName, { when: state.deadline_at });
  return { ok: true };
}

export function browserRevisitPageMatches(platform, actualUrl, targetUrl) {
  try {
    const actual = new URL(actualUrl);
    const target = new URL(targetUrl);
    if (actual.pathname.replace(/\/$/, "") !== target.pathname.replace(/\/$/, "")) return false;
    const hosts = {
      instagram: new Set(["instagram.com", "www.instagram.com"]),
      threads: new Set(["threads.com", "www.threads.com", "threads.net", "www.threads.net"]),
      tiktok: new Set(["tiktok.com", "www.tiktok.com"]),
      x: new Set(["x.com", "www.x.com", "twitter.com", "www.twitter.com"]),
      facebook: new Set(["facebook.com", "www.facebook.com", "m.facebook.com"]),
      lemon8: new Set(["lemon8-app.com", "www.lemon8-app.com", "lemon8.com", "www.lemon8.com"]),
    }[platform];
    const hostOk = (hostname) => {
      const host = hostname.toLowerCase();
      if (hosts && hosts.has(host)) return true;
      if (platform === "instagram") return host === "instagram.com" || host.endsWith(".instagram.com");
      if (platform === "facebook") return host === "facebook.com" || host.endsWith(".facebook.com");
      return false;
    };
    return hostOk(actual.hostname) && hostOk(target.hostname);
  } catch (_) {
    return false;
  }
}

export async function clearBrowserRevisitWatchdog(queue, platform, contentId, claimAttempt) {
  const key = browserRevisitWatchdogKey(queue, platform);
  const alarmName = browserRevisitWatchdogAlarm(queue, platform);
  const values = await chrome.storage.local.get(key);
  const existing = values && values[key];
  if (!existing || existing.content_id !== contentId || existing.claim_attempt !== claimAttempt) return;
  await chrome.storage.local.remove(key);
  await chrome.alarms.clear(alarmName);
}

export async function runBrowserRevisitWatchdog(alarmName) {
  const suffix = alarmName.slice(BROWSER_REVISIT_WATCHDOG_PREFIX.length);
  const [queue, ...platformParts] = suffix.split(":");
  const platform = platformParts.join(":");
  if (!new Set(["browser", "tiktok"]).has(queue) || !platform) return;
  const key = browserRevisitWatchdogKey(queue, platform);
  const values = await chrome.storage.local.get(key);
  const state = values && values[key];
  if (!state || !state.content_id) return;
  if (Number(state.deadline_at || 0) > Date.now()) {
    await chrome.alarms.create(alarmName, { when: state.deadline_at });
    return;
  }

  let tab = null;
  try { tab = await chrome.tabs.get(state.tab_id); } catch (_) {}
  if (tab && browserRevisitPageMatches(platform, tab.url || "", state.url || "")) {
    if (state.phase === "navigation") {
      const next = { ...state, phase: "result", deadline_at: Date.now() + BROWSER_REVISIT_RESULT_TIMEOUT_MS };
      await chrome.storage.local.set({ [key]: next });
      await chrome.alarms.create(alarmName, { when: next.deadline_at });
      return;
    }
  }

  const reason = !tab
    ? "detail_tab_closed"
    : state.phase === "navigation" ? "detail_navigation_timeout" : "detail_result_timeout";
  try {
    const base = await ingestBase();
    const endpoint = queue === "tiktok"
      ? "/social/tiktok-revisit-result"
      : "/social/browser-revisit-result";
    const body = {
      content_id: state.content_id,
      claim_attempt: state.claim_attempt,
      status: "failed",
      reason,
      username: null,
    };
    if (queue === "browser") body.platform = platform;
    const result = await postJsonWithTimeout(
      base + endpoint,
      withExtensionVersion(body),
      SCRAPER_HEARTBEAT_TIMEOUT_MS,
    );
    const responseBody = JSON.parse(result.body || "{}");
    if (result.response && result.response.ok && responseBody.accepted === false) {
      await clearBrowserRevisitWatchdog(queue, platform, state.content_id, state.claim_attempt);
      return;
    }
    if (result.response && result.response.ok && responseBody.accepted === true) {
      await clearBrowserRevisitWatchdog(queue, platform, state.content_id, state.claim_attempt);
      await log("warn", `${platform} browser revisit lease released after ${reason}`);
      return;
    }
  } catch (_) {}
  const next = { ...state, deadline_at: Date.now() + 2 * 60 * 1000 };
  await chrome.storage.local.set({ [key]: next });
  await chrome.alarms.create(alarmName, { when: next.deadline_at });
}
