// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// scrape_config: 7 top-level defs moved verbatim; imports added for cross-module refs.
import { controlBase } from "../shared/ingest_client.js";

// ---- config --------------------------------------------------------------
// Cached copies of the endpoint bases. Chrome.storage.local.get from inside
// the message handler was measurable overhead under high heartbeat load: 7
// content tabs × loopStatus every 20–30s + per-cycle logs all serialized on
// storage.local — the SW frequently could not reply within the content
// script's 15 s loopStatus / 10 s log budget and content fell back to
// content_direct. Reading from a module-scope cache eliminates that hop.
// The values are only writable via popup.js (chrome.storage.local.set);
// shared/ingest_client attaches a storage.onChanged listener at module load
// to keep its cache honest.

// ---- rotator scrape-config gate (plan G3-wire) ---------------------------
// The activity rotator disables browser platforms in collection_schedules so
// the shared Chrome never scrapes more than the rotation width at once. The SW
// polls /social/scrape-config and skips forcing cycles for disabled platforms.
// Fail-open: any fetch/parse error leaves the platform enabled (never silently
// stop collection on a transient control-plane blip).
export let _scrapeConfigDisabled = new Set();

export let _scrapeConfig = {};

export let _scrapeConfigFetchedAt = 0;

export const SCRAPE_CONFIG_TTL_MS = 60000;

export async function refreshScrapeConfig() {
  const now = Date.now();
  if (now - _scrapeConfigFetchedAt < SCRAPE_CONFIG_TTL_MS) return;
  _scrapeConfigFetchedAt = now;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 5000);
    let r;
    try {
      const control = await controlBase();
      r = await fetch(control + "/social/scrape-config", { signal: ctrl.signal });
    } finally {
      clearTimeout(timer);
    }
    if (!r || !r.ok) return;
    const cfg = await r.json();
    if (cfg && Array.isArray(cfg.disabled)) {
      _scrapeConfigDisabled = new Set(cfg.disabled.map((s) => String(s).toLowerCase()));
      _scrapeConfig = cfg;
    }
  } catch (e) {
    /* fail-open: keep previous set; never block scraping on a config blip */
  }
}

export async function platformScrapeDisabled(platform) {
  const id = platform && (platform.id || platform.label);
  if (!id) return false;
  try {
    await refreshScrapeConfig();
  } catch (e) { return false; }
  return _scrapeConfigDisabled.has(String(id).toLowerCase());
}

export async function browserPacing(platform, account) {
  await refreshScrapeConfig();
  const id = String(platform || "").toLowerCase();
  const profile = (_scrapeConfig && _scrapeConfig.profiles && _scrapeConfig.profiles[id]) || {};
  if (_scrapeConfigDisabled.has(id) || profile.mode === "manually_paused") {
    return { ...profile, mode: "manually_paused", volume_profile: "manual" };
  }
  const recoveryProfiles = Array.isArray(profile.recovery_profiles) ? profile.recovery_profiles : [];
  const normalizedAccount = String(account || "").trim().replace(/^@/, "").toLowerCase();
  const recovery = recoveryProfiles.find((value) => {
    const profileAccount = String(value && value.account || "").trim().replace(/^@/, "").toLowerCase();
    return !profileAccount || !normalizedAccount || profileAccount === normalizedAccount;
  });
  if (recovery) {
    return {
      ...profile,
      mode: recovery.exit_required ? "recovery_exit" : "restricted",
      volume_profile: "restricted",
    };
  }
  if (profile.mode === "cooldown") return { ...profile, mode: "cooldown", volume_profile: "cooldown" };
  return { ...profile, mode: "near_live_polling", volume_profile: "normal" };
}
