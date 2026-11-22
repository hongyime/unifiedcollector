// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// loops: 7 top-level defs moved verbatim; imports added for cross-module refs.
import { platformForTabUrl } from "./heartbeat.js";
import { platformUrlPatterns, scraperUrlPatterns } from "./page_recovery.js";
import { ingestBase } from "../shared/ingest_client.js";
import { log } from "../shared/log.js";
import { setStatus } from "./status.js";
import { isNoReceiverError, isTabMessageTimeout, recordServiceWorkerRecovery, refreshContentScriptAndNudge, refreshTabForMissingContentScript, sendTabMessageWithTimeout } from "./tab_recovery.js";
import { _mainWorldHookHostRe, closeDuplicateScraperTabRows, scraperTabSelection, selectCanonicalScraperTabRows } from "./tab_selection.js";

          // re-nudge any open scraper tab whose loop died
export const KICK_DEBOUNCE_MS = 30000;

// Nudge every open scraper tab to ensure its continuous loop is running. The tab
// auto-starts the loop on load; this only RESPAWNS it if it died (page reload,
// crash) or the service worker had been asleep. No scrape cadence here — pacing
// lives inside the loop (rate-limited + jittered).
export async function ensureLoops(reason) {
  const forceCycle = /browser_content_stale|manual|scrape|tabs_page|stale/i.test(reason || "");
  const allTabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
  const selection = selectCanonicalScraperTabRows(allTabs);
  await closeDuplicateScraperTabRows(selection.duplicateRows, `ensure_loops:${reason || "nudge"}`);
  const { activeIds } = await scraperTabSelection();
  const tabs = selection.rows.filter((row) => activeIds.has(row.platform.id)).map((row) => row.tab);
  if (!selection.seen) {
    await log("warn", `no scraper tab open — paused (${reason}). Open one via 🗂 Manage social tabs.`);
    await setStatus({ loopRunning: false });
    return false;
  }
  for (const t of tabs) {
    try {
      const host = new URL(t.url || "").hostname;
      if (_mainWorldHookHostRe.test(host) && chrome.scripting && chrome.scripting.executeScript) {
        await chrome.scripting.executeScript({
          target: { tabId: t.id, allFrames: false },
          files: ["dist/inject.js"],
          world: "MAIN",
        });
      }
    } catch (e) {
      const error = String(e && e.message ? e.message : e);
      // Chromium's own network-error document (chrome-error://chromewebdata/)
      // is not an extension-injectable page. The manifest hook also cannot run
      // there; keep it visible as an expected skip instead of a scraper error.
      const browserErrorDocument = /chrome-error:\/\/|chromewebdata/i.test(error);
      await log(
        browserErrorDocument ? "info" : "warn",
        `${browserErrorDocument ? "page hook injection skipped on browser error page" : "page hook inject failed"} for tab ${t.id}: ${error}`,
      );
    }
    try {
      await sendTabMessageWithTimeout(t.id, { type: forceCycle ? "scrapeCycle" : "ensureLoop", reason });
    } catch (firstErr) {
      try {
        const platform = platformForTabUrl(t.url);
        const receiverMissing = isNoReceiverError(firstErr);
        const messageTimedOut = isTabMessageTimeout(firstErr);
        const cycleError = firstErr && firstErr.message ? firstErr.message : String(firstErr);
        if (platform && messageTimedOut) {
          const base = await ingestBase();
          const refreshed = await refreshContentScriptAndNudge(base, t, platform, "ensure_loop_message_timeout", {
            cycle_error: cycleError,
            message_timeout: true,
            recovery: "message_timeout_manifest_refresh",
          });
          await recordServiceWorkerRecovery(base, t, platform, "content_script_message_timeout", reason || "ensure_loop", {
            cycle_error: cycleError,
            message_timeout: true,
            refresh_attempted: true,
            refresh_ok: refreshed || null,
          });
          await log("warn", `${platform.label}: scraper tab did not answer loop nudge within timeout; ${refreshed ? "hard-refreshed tab for manifest content script" : "leaving it open to finish current work"} (${reason})`);
        } else if (platform && receiverMissing) {
          const base = await ingestBase();
          await recordServiceWorkerRecovery(base, t, platform, "content_script_missing_refresh", reason || "ensure_loop", {
            cycle_error: cycleError,
            no_receiver: true,
          });
          const reloaded = await refreshTabForMissingContentScript(base, t, platform, "ensure_loop_receiver_missing", {
            cycle_error: cycleError,
            no_receiver: true,
          });
          if (reloaded) {
            await log("warn", `content scraper missing in tab ${t.id}; hard-refreshed so manifest content.js can reload (${reason})`);
          } else {
            await log("warn", `content scraper missing in tab ${t.id}; hard-refresh already debounced (${reason})`);
          }
        } else {
          throw firstErr;
        }
      } catch (e) {
        await log("warn", `content scraper recovery failed for tab ${t.id}: ${e && e.message ? e.message : e}`);
      }
    }
  }
  return true;
}

export const lastKick = {};

export async function kick(reason, tabId) {
  if (tabId != null) {
    const now = Date.now();
    if (lastKick[tabId] && now - lastKick[tabId] < KICK_DEBOUNCE_MS) return;
    lastKick[tabId] = now;
  }
  return ensureLoops(reason);
}

// ---- social tab launcher -------------------------------------------------
export async function tabForPlatform(p) {
  const tabs = await chrome.tabs.query({ url: platformUrlPatterns(p) });
  const selection = selectCanonicalScraperTabRows(tabs);
  // Status/recovery actions must use the same owner as the tab-maintenance
  // path; otherwise an arbitrary duplicate can receive the reload.
  await closeDuplicateScraperTabRows(selection.duplicateRows, `tabForPlatform:${p.id}`);
  const row = selection.rows.find((candidate) => candidate.platform.id === p.id);
  return row ? row.tab : null;
}

export async function isLoggedIn(p) {
  if (!p.cookie) return null;
  try {
    const urls = [p.cookieUrl, ...(p.cookieUrls || [])].filter(Boolean);
    for (const url of urls) {
      const c = await chrome.cookies.get({ url, name: p.cookie });
      if (c && c.value) return true;
    }
    return false;
  }
  catch (e) { return null; }
}

export async function platformStatuses() {
  const out = [];
  for (const p of globalThis.UC_PLATFORMS || []) {
    const tab = await tabForPlatform(p);
    out.push({ id: p.id, label: p.label, url: p.url, host: p.host, scraper: !!p.scraper, noLogin: !!p.noLogin, tabOpen: !!tab, tabId: tab ? tab.id : null, loggedIn: await isLoggedIn(p) });
  }
  return out;
}
