// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// page_recovery: 24 top-level defs moved verbatim; imports added for cross-module refs.
import { SCRAPER_HEARTBEAT_TIMEOUT_MS, withExtensionVersion } from "./heartbeat.js";
import { postJsonWithTimeout } from "../shared/ingest_client.js";
import { log } from "../shared/log.js";
import { hardRefreshNavigationUrl, scheduleMaybeForceScrapeCycle } from "./tab_recovery.js";
import { AUTO_SCRAPER_PLATFORM_IDS } from "./tab_selection.js";
import { _randBetween } from "./util.js";

export const PAGE_RECOVERY_PREFIX = "uc-page-recovery:";

export const PAGE_RECOVERY_STATE_KEY = "ucPageRecovery";

export const PAGE_RECOVERY_MAX_ATTEMPTS = 3;

export const PAGE_RECOVERY_STATE_TTL_MS = 60 * 60 * 1000;

export const PAGE_RECOVERY_LIMIT_COOLDOWN_MS = 30 * 60 * 1000;

export const PAGE_RECOVERY_LIMIT_COOLDOWN_MS_BY_PLATFORM = {
  x: 10 * 60 * 1000,
};

export const PAGE_RECOVERY_DELAY_WINDOWS_MS = [
  [45000, 150000],
  [240000, 480000],
  [720000, 1200000],
];

// scraper hosts that have a content-script scraper
export function scraperPlatforms() {
  return (globalThis.UC_PLATFORMS || []).filter((p) => p.scraper && AUTO_SCRAPER_PLATFORM_IDS.has(p.id));
}

export function platformHosts(p) {
  const hosts = [p && p.host, ...((p && p.aliasHosts) || [])]
    .filter(Boolean)
    .map((host) => String(host).trim().toLowerCase().replace(/^www\./, ""))
    .filter(Boolean);
  // Registry entries may use the bare domain while the canonical URL or the
  // user's logged-in tab uses www (or vice versa). Match both forms so tab
  // status, cleanup, and scraper heartbeats all see the same platform tab.
  return Array.from(new Set(hosts.flatMap((host) => [host, `www.${host}`])));
}

export function platformUrlPatterns(p) {
  return platformHosts(p).map((host) => `https://${host}/*`);
}

export function scraperUrlPatterns() { return scraperPlatforms().flatMap(platformUrlPatterns); }

export function platformById(id) { return scraperPlatforms().find((p) => p.id === id) || null; }

export function pageRecoveryAlarm(tabId) { return PAGE_RECOVERY_PREFIX + String(tabId); }

export function normalizeRecoveryUrl(url) {
  try {
    const u = new URL(url || "");
    return u.origin + u.pathname.replace(/\/$/, "");
  } catch (e) {
    return String(url || "").split("#")[0].split("?")[0];
  }
}

export async function pageRecoveryState() {
  const { [PAGE_RECOVERY_STATE_KEY]: cur = {} } = await chrome.storage.local.get(PAGE_RECOVERY_STATE_KEY);
  const state = cur && typeof cur === "object" ? cur : {};
  const now = Date.now();
  let changed = false;
  for (const [key, rec] of Object.entries(state)) {
    const lastSeenAt = Number(rec && rec.lastSeenAt || 0);
    if (!lastSeenAt || now - lastSeenAt > PAGE_RECOVERY_STATE_TTL_MS) {
      delete state[key];
      changed = true;
    }
  }
  if (changed) await savePageRecoveryState(state);
  return state;
}

export async function savePageRecoveryState(state) {
  await chrome.storage.local.set({ [PAGE_RECOVERY_STATE_KEY]: state || {} });
}

export async function clearPageRecoveryState(reason) {
  try { await chrome.storage.local.remove(PAGE_RECOVERY_STATE_KEY); } catch (e) {}
  try {
    const alarms = await chrome.alarms.getAll();
    for (const alarm of alarms || []) {
      if (alarm && alarm.name && alarm.name.startsWith(PAGE_RECOVERY_PREFIX)) {
        await chrome.alarms.clear(alarm.name);
      }
    }
  } catch (e) {}
  await log("info", `page recovery state reset (${reason})`);
}

export function shouldResetPageRecoveryForStartup(reason) {
  return /^(installed|manual_extension_reload|version_changed)$/i.test(String(reason || ""));
}

export async function clearPageRecoveryForTab(tabId, reason) {
  if (tabId == null) return;
  const state = await pageRecoveryState();
  const prefix = String(tabId) + ":";
  let changed = false;
  for (const key of Object.keys(state)) {
    if (key.startsWith(prefix)) {
      delete state[key];
      changed = true;
    }
  }
  if (changed) {
    try { await chrome.alarms.clear(pageRecoveryAlarm(tabId)); } catch (e) {}
    await savePageRecoveryState(state);
    if (reason) await log("info", `page recovery cleared for tab ${tabId} (${reason})`);
  }
}

export async function recordPageHealth(base, msg, sender, extra = {}) {
  try {
    const result = await postJsonWithTimeout(base + "/social/browser-heartbeat", withExtensionVersion({
      platform: msg.platform,
      label: msg.label || msg.platform,
      running: true,
      url: msg.url || (sender && sender.tab && sender.tab.url) || null,
      tab_id: sender && sender.tab ? sender.tab.id : null,
      health_status: msg.status || "unknown",
      health_reason: msg.reason || null,
      page_title: msg.title || null,
      text_sample: msg.sample || null,
      content_counts: msg.content_counts || null,
      cycle_reason: msg.cycle_reason || null,
      cycle_targets: msg.cycle_targets ?? null,
      cycle_saved: msg.cycle_saved ?? null,
      cycle_discovered: msg.cycle_discovered ?? null,
      cycle_error: msg.cycle_error || null,
      cooldown_left_ms: msg.cooldown_left_ms ?? null,
      loop_running: msg.loop_running ?? null,
      one_shot_running: msg.one_shot_running ?? null,
      one_shot_age_ms: msg.one_shot_age_ms ?? null,
      scrape_pass_running: msg.scrape_pass_running ?? null,
      scrape_pass_age_ms: msg.scrape_pass_age_ms ?? null,
      scrape_pass_reason: msg.scrape_pass_reason ?? null,
      stale_after_ms: msg.stale_after_ms ?? null,
      one_shot_timeout: msg.one_shot_timeout ?? null,
      timeout_ms: msg.timeout_ms ?? null,
      ...extra,
    }), SCRAPER_HEARTBEAT_TIMEOUT_MS);
    const tab = sender && sender.tab ? sender.tab : null;
    const platform = platformById(msg.platform) || { id: msg.platform, label: msg.label || msg.platform };
    await scheduleMaybeForceScrapeCycle(tab, platform, result && result.body, msg.reason || msg.status || "page_health", base);
  } catch (e) {}
}

export function recoveryDelayMs(attempt, platformId) {
  const windows = platformId === "x"
    ? [
        [8000, 20000],
        [45000, 90000],
        [120000, 240000],
      ]
    : PAGE_RECOVERY_DELAY_WINDOWS_MS;
  const idx = Math.min(windows.length - 1, Math.max(0, attempt - 1));
  const win = windows[idx];
  return _randBetween(win[0], win[1]);
}

export function recoveryLimitCooldownMs(platformId) {
  return PAGE_RECOVERY_LIMIT_COOLDOWN_MS_BY_PLATFORM[platformId] || PAGE_RECOVERY_LIMIT_COOLDOWN_MS;
}

export async function schedulePageRecovery(base, msg, sender) {
  const tab = sender && sender.tab;
  if (!tab || tab.id == null) return { ok: false, reason: "no_sender_tab" };
  const platform = platformById(msg.platform);
  if (!platform) return { ok: false, reason: "unknown_platform" };
  const url = msg.url || tab.url || "";
  const current = normalizeRecoveryUrl(url);
  const state = await pageRecoveryState();
  const key = `${tab.id}:${platform.id}`;
  const prev = state[key] || {};
  const samePage = prev.url === current;
  let attempts = samePage ? Number(prev.attempts || 0) : 0;
  const now = Date.now();
  const limitUntil = samePage ? Number(prev.limitUntil || 0) : 0;

  if (limitUntil && now < limitUntil) {
    const delay = Math.max(0, limitUntil - now);
    await recordPageHealth(base, msg, sender, {
      recovery_scheduled: false,
      recovery_attempt: attempts,
      recovery_limit: true,
      recovery_delay_ms: delay,
    });
    return {
      ok: true,
      scheduled: false,
      reason: "attempt_limit_cooling",
      attempt: attempts,
      cooldown_mins: Math.ceil(delay / 60000),
    };
  }
  if (limitUntil && now >= limitUntil) {
    attempts = 0;
  }

  if (prev.nextAt && now < prev.nextAt && samePage) {
    const delay = Math.max(0, prev.nextAt - now);
    await recordPageHealth(base, msg, sender, {
      recovery_scheduled: true,
      recovery_pending: true,
      recovery_attempt: attempts,
      recovery_delay_ms: delay,
    });
    return { ok: true, scheduled: true, pending: true, attempt: attempts, delay_ms: delay };
  }

  if (attempts >= PAGE_RECOVERY_MAX_ATTEMPTS) {
    const cooldownMs = recoveryLimitCooldownMs(platform.id);
    state[key] = { ...prev, url: current, lastSeenAt: now, limitLogged: true, limitUntil: now + cooldownMs };
    await savePageRecoveryState(state);
    if (!prev.limitLogged) {
      await log("warn", `${platform.label} page recovery hit attempt limit on tab ${tab.id}; cooling instead of reload loop`);
    }
    await recordPageHealth(base, msg, sender, {
      recovery_scheduled: false,
      recovery_attempt: attempts,
      recovery_limit: true,
      recovery_delay_ms: cooldownMs,
    });
    return { ok: true, scheduled: false, reason: "attempt_limit", attempt: attempts, cooldown_mins: Math.ceil(cooldownMs / 60000) };
  }

  attempts += 1;
  const delay = recoveryDelayMs(attempts, platform.id);
  state[key] = {
    url: current,
    platform: platform.id,
    attempt: attempts,
    attempts,
    nextAt: now + delay,
    lastSeenAt: now,
    reason: msg.reason || "recoverable_error_shell",
    title: msg.title || "",
  };
  await savePageRecoveryState(state);
  await chrome.alarms.create(pageRecoveryAlarm(tab.id), { when: now + delay });
  await log("warn", `${platform.label} page looks stuck (${msg.reason || "error shell"}); reload scheduled in ${Math.round(delay / 1000)}s (attempt ${attempts}/${PAGE_RECOVERY_MAX_ATTEMPTS})`);
  await recordPageHealth(base, msg, sender, {
    recovery_scheduled: true,
    recovery_attempt: attempts,
    recovery_delay_ms: delay,
  });
  return { ok: true, scheduled: true, attempt: attempts, delay_ms: delay };
}

export async function runPageRecovery(tabId) {
  const state = await pageRecoveryState();
  const prefix = String(tabId) + ":";
  const key = Object.keys(state).find((k) => k.startsWith(prefix));
  if (!key) return;
  const rec = state[key] || {};
  try {
    const tab = await chrome.tabs.get(Number(tabId));
    const current = normalizeRecoveryUrl(tab && tab.url);
    if (!tab || current !== rec.url) {
      delete state[key];
      await savePageRecoveryState(state);
      await log("info", `page recovery skipped for tab ${tabId}; tab moved away`);
      return;
    }
    // If this platform gets home-URL hard-refresh on forced-cycle recovery, use
    // the same navigation strategy here: reloading a lemon8 SPA that's showing
    // "Not found" on /feed/food?region=sg just re-serves the same broken page.
    // Navigating to platform.url gets the tab back onto a fresh feed root.
    const platform = platformById(rec.platform);
    const navTarget = platform ? hardRefreshNavigationUrl(platform, tab.url, Date.now()) : null;
    if (navTarget) {
      await chrome.tabs.update(Number(tabId), { url: navTarget });
      state[key] = { ...rec, nextAt: 0, lastReloadAt: Date.now(), lastNavTo: navTarget };
      await savePageRecoveryState(state);
      await log("warn", `page recovery navigated tab ${tabId} to ${navTarget} (${rec.platform || "scraper"}, attempt ${rec.attempts || rec.attempt || 1})`);
      return;
    }
    await chrome.tabs.reload(Number(tabId), { bypassCache: true });
    state[key] = { ...rec, nextAt: 0, lastReloadAt: Date.now() };
    await savePageRecoveryState(state);
    await log("warn", `page recovery reloaded tab ${tabId} (${rec.platform || "scraper"}, attempt ${rec.attempts || rec.attempt || 1})`);
  } catch (e) {
    delete state[key];
    await savePageRecoveryState(state);
  }
}
