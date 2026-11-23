// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// tab_selection: 36 top-level defs moved verbatim; imports added for cross-module refs.
import { extensionVersion, platformForTabUrl } from "./heartbeat.js";
import { platformUrlPatterns, scraperPlatforms, scraperUrlPatterns } from "./page_recovery.js";
import { _scrapeConfig, refreshScrapeConfig } from "./scrape_config.js";
import { log } from "../shared/log.js";
import { _humanGap, _sleep } from "./util.js";

export async function autoTabsEnabled() {
  const { ucAutoTabs } = await chrome.storage.local.get("ucAutoTabs");
  return ucAutoTabs !== false; // default ON
}

export let _tabsOpInProgress = false;

// Open exactly the missing scraper tabs in the background, ONE at a time with a
// gap so we never spam tabs or spike CPU. Robust dedup by host+path-prefix means a
// tab is never duplicated.
export const _tpath = (u) => { try { return new URL(u).pathname.split("?")[0].replace(/\/$/, "") || "/"; } catch (e) { return "/"; } };

export const _mainWorldHookHostRe = /(^|\.)((instagram|tiktok|strava)\.com|threads\.com|x\.com|twitter\.com|facebook\.com)$/i;

export const SCRAPER_TABS_PINNED_KEY = "ucPinScraperTabs";

export const AUTO_SCRAPER_PLATFORM_IDS = new Set(["instagram", "threads", "tiktok", "lemon8", "x", "facebook", "strava", "pinterest", "snapchat", "airbnb", "bluesky", "youtube"]);

export const BROWSER_RESIDENT_TAB_LIMIT = 6;

export const BROWSER_RESIDENT_PLATFORM_IDS = ["x", "instagram", "facebook", "threads", "tiktok", "lemon8", "strava", "pinterest", "snapchat", "airbnb"];

export const BROWSER_ROTATION_PLATFORM_IDS = ["lemon8", "threads", "pinterest", "snapchat", "airbnb", "x"];

export const BROWSER_RESIDENT_FALLBACK_IDS = ["x", "instagram"];

export const SCRAPER_TAB_FAILOVER_KEY = "ucScraperTabFailover";

export const SCRAPER_TAB_QUARANTINE_MS = 30 * 60 * 1000;

export const SCRAPER_TAB_PROBE_MS = 15 * 60 * 1000;

export function configTime(value) {
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

export function scraperFailoverProfile(id) {
  return (_scrapeConfig && _scrapeConfig.profiles && _scrapeConfig.profiles[id]) || {};
}

export function scraperFailoverRetryAt(profile, now) {
  return Math.max(now + SCRAPER_TAB_QUARANTINE_MS, configTime(profile.tab_retry_at));
}

export async function scraperFailoverState() {
  try {
    const stored = await chrome.storage.local.get(SCRAPER_TAB_FAILOVER_KEY);
    const value = stored && stored[SCRAPER_TAB_FAILOVER_KEY] || {};
    return {
      residents: Array.isArray(value.residents) ? value.residents.filter((id) => BROWSER_RESIDENT_PLATFORM_IDS.includes(id)) : [],
      evictions: value.evictions && typeof value.evictions === "object" ? value.evictions : {},
      probe: value.probe && BROWSER_RESIDENT_PLATFORM_IDS.includes(value.probe.platform) ? value.probe : null,
    };
  } catch (e) {
    return { residents: [], evictions: {}, probe: null };
  }
}

async function _adoptExistingResidentTabs(state) {
  if (!state.residents.length) {
    try {
      const existing = selectCanonicalScraperTabRows(await chrome.tabs.query({ url: scraperUrlPatterns() }));
      for (const id of BROWSER_RESIDENT_PLATFORM_IDS) {
        if (state.residents.length >= BROWSER_RESIDENT_TAB_LIMIT) break;
        if (existing.rows.some((row) => row.platform.id === id)) state.residents.push(id);
      }
      return state.residents.length > 0;
    } catch (e) {}
  }
  return false;
}

async function _rebuildResidentTabs(state, now, eligible, rotationPriority, nonRotationLimit) {
  let changed = false;
  const residents = [];
  let nonRotationResidents = 0;
  for (const id of state.residents) {
    if (residents.includes(id)) { changed = true; continue; }
    const prioritized = rotationPriority.has(id);
    if (eligible(id) && !state.evictions[id] && (prioritized || nonRotationResidents < nonRotationLimit)) {
      residents.push(id);
      if (!prioritized) nonRotationResidents++;
      continue;
    }
    const profile = scraperFailoverProfile(id);
    if (!state.evictions[id]) {
      state.evictions[id] = {
        evicted_at: now,
        reason: String(profile.tab_block_reason || "ineligible"),
        retry_at: scraperFailoverRetryAt(profile, now),
      };
      await log("warn", `scraper tab evicted: ${id} (${state.evictions[id].reason})`);
    }
    changed = true;
  }
  const residentCandidates = [
    ...BROWSER_ROTATION_PLATFORM_IDS.filter((id) => rotationPriority.has(id)),
    ...BROWSER_RESIDENT_PLATFORM_IDS.filter((id) => !rotationPriority.has(id)),
  ];
  for (const id of residentCandidates) {
    if (residents.length >= BROWSER_RESIDENT_TAB_LIMIT) break;
    if (eligible(id) && !state.evictions[id] && !residents.includes(id)) {
      residents.push(id);
      if (Object.keys(state.evictions).length) await log("info", `scraper tab replacement selected: ${id}`);
    }
  }
  if (residents.join(",") !== state.residents.join(",")) changed = true;
  return { residents, changed };
}

async function _resolveRecoveryProbe(state, now, enabled) {
  let changed = false;
  let probeId = state.probe && state.probe.platform;
  if (probeId) {
    const profile = scraperFailoverProfile(probeId);
    const startedAt = Number(state.probe.started_at || 0);
    const observedAt = configTime(profile.health_checked_at);
    const succeededAt = configTime(profile.last_successful_check_at);
    if (succeededAt > startedAt && profile.tab_eligible !== false) {
      const completedProbeId = probeId;
      delete state.evictions[probeId];
      state.probe = null;
      probeId = null;
      changed = true;
      await log("info", `scraper recovery probe succeeded: ${completedProbeId}`);
    } else if (observedAt > startedAt && profile.tab_eligible === false) {
      state.evictions[probeId] = {
        evicted_at: now,
        reason: String(profile.tab_block_reason || "unhealthy"),
        retry_at: scraperFailoverRetryAt(profile, now),
      };
      state.probe = null;
      await log("warn", `scraper recovery probe re-quarantined: ${probeId} (${state.evictions[probeId].reason})`);
      probeId = null;
      changed = true;
    } else if (now - startedAt >= SCRAPER_TAB_PROBE_MS) {
      state.evictions[probeId] = {
        evicted_at: now,
        reason: "probe_timeout",
        retry_at: scraperFailoverRetryAt(profile, now),
      };
      state.probe = null;
      await log("warn", `scraper recovery probe timed out: ${probeId}`);
      probeId = null;
      changed = true;
    }
  }
  if (!probeId) {
    const candidate = BROWSER_RESIDENT_PLATFORM_IDS.find((id) => {
      const eviction = state.evictions[id];
      const profile = scraperFailoverProfile(id);
      return eviction && Number(eviction.retry_at || 0) <= now && enabled.has(id) && profile.mode !== "manually_paused";
    });
    if (candidate) {
      state.probe = { platform: candidate, started_at: now };
      probeId = candidate;
      changed = true;
      await log("info", `scraper recovery probe started: ${candidate}`);
    }
  }
  return { probeId, changed };
}

export async function scraperTabSelection() {
  await refreshScrapeConfig();
  const enabled = _scrapeConfig && _scrapeConfig.fail_open !== true && Array.isArray(_scrapeConfig.enabled)
    ? new Set(_scrapeConfig.enabled.map((id) => String(id).toLowerCase()))
    : null;
  if (!enabled) {
    return { residentIds: new Set(BROWSER_RESIDENT_FALLBACK_IDS), activeIds: new Set(BROWSER_RESIDENT_FALLBACK_IDS), probeId: null };
  }
  const now = Date.now();
  const state = await scraperFailoverState();
  let changed = false;
  const eligible = (id) => enabled.has(id) && scraperFailoverProfile(id).tab_eligible !== false;
  const rotationPriority = new Set(
    _scrapeConfig.fail_open === true
      ? []
      : BROWSER_ROTATION_PLATFORM_IDS.filter((id) => eligible(id)),
  );
  const nonRotationLimit = Math.max(0, BROWSER_RESIDENT_TAB_LIMIT - rotationPriority.size);
  changed = (await _adoptExistingResidentTabs(state)) || changed;

  const { residents, changed: rebuildChanged } = await _rebuildResidentTabs(state, now, eligible, rotationPriority, nonRotationLimit);
  changed = changed || rebuildChanged;
  state.residents = residents;
  const { probeId, changed: probeChanged } = await _resolveRecoveryProbe(state, now, enabled);
  changed = changed || probeChanged;
  if (changed) await chrome.storage.local.set({ [SCRAPER_TAB_FAILOVER_KEY]: state });
  const residentIds = new Set(state.residents);
  const activeIds = new Set(state.residents);
  if (probeId) activeIds.add(probeId);
  return { residentIds, activeIds, probeId };
}


export async function scraperTabsPinned() {
  try {
    const stored = await chrome.storage.local.get([SCRAPER_TABS_PINNED_KEY]);
    return stored && stored[SCRAPER_TABS_PINNED_KEY] === true;
  } catch (e) {
    return false;
  }
}

export function scraperTabRows(tabs) {
  return (tabs || [])
    .map((tab) => ({ tab, platform: platformForTabUrl(tab.url) }))
    .filter((row) => row.platform && row.tab && row.tab.id != null);
}

export function platformTargetPaths(platform) {
  return platform && platform.url ? [_tpath(platform.url)] : [];
}

export function canonicalTabKey(row) {
  const platform = row && row.platform;
  return platform && platform.id ? platform.id : null;
}

export function canonicalTabScore(row) {
  const tab = row && row.tab ? row.tab : {};
  const path = _tpath(tab.url || "");
  const targets = platformTargetPaths(row && row.platform);
  let score = 0;
  if (tab.status === "complete") score += 50;
  if (!tab.discarded) score += 20;
  if (tab.active) score += 10;
  if (targets.some((want) => path === want)) score += 8;
  else if (targets.some((want) => want !== "/" && path.startsWith(want))) score += 4;
  return score;
}

export function selectCanonicalScraperTabRows(tabs) {
  const rows = scraperTabRows(tabs);
  const byKey = new Map();
  const duplicateRows = [];
  let skipped = 0;
  for (const row of rows) {
    const key = canonicalTabKey(row);
    if (!key) continue;
    const cur = byKey.get(key);
    if (!cur) {
      byKey.set(key, row);
      continue;
    }
    const rowScore = canonicalTabScore(row);
    const curScore = canonicalTabScore(cur);
    const rowId = Number(row.tab && row.tab.id);
    const curId = Number(cur.tab && cur.tab.id);
    if (rowScore > curScore || (rowScore === curScore && rowId < curId)) {
      duplicateRows.push(cur);
      byKey.set(key, row);
    } else {
      duplicateRows.push(row);
    }
    skipped++;
  }
  const selected = Array.from(byKey.values()).sort((a, b) => {
    const ak = canonicalTabKey(a) || "";
    const bk = canonicalTabKey(b) || "";
    return ak.localeCompare(bk);
  });
  return { rows: selected, seen: rows.length, skipped, duplicateRows };
}

export async function closeDuplicateScraperTabRows(rows, reason) {
  let closed = 0;
  let errors = 0;
  for (const row of rows || []) {
    const id = Number(row && row.tab && row.tab.id);
    if (!Number.isFinite(id)) continue;
    try {
      await chrome.tabs.remove(id);
      closed++;
    } catch (e) {
      errors++;
    }
  }
  if (closed || errors) {
    await log("info", `closed ${closed} duplicate scraper tab(s), ${errors} close error(s) (${reason})`);
  }
  return { closed, errors };
}

export function isExtensionControlTabUrl(url) {
  try {
    const u = new URL(String(url || ""));
    return u.protocol === "chrome-extension:" && u.pathname === "/tabs.html";
  } catch (e) {
    return false;
  }
}

export function isBlankStartupTab(tab) {
  const url = String((tab && tab.url) || "");
  return (url === "about:blank" || url === "chrome://newtab/") && !tab.active;
}

export async function closeDuplicateExtensionControlTabs(reason) {
  let closed = 0;
  let unpinned = 0;
  let errors = 0;
  try {
    const tabs = (await chrome.tabs.query({})) || [];
    const controls = tabs
      .filter((tab) => tab && tab.id != null && isExtensionControlTabUrl(tab.url))
      .sort((a, b) => {
        const aCurrent = String(a.url || "").startsWith(chrome.runtime.getURL("tabs.html"));
        const bCurrent = String(b.url || "").startsWith(chrome.runtime.getURL("tabs.html"));
        if (aCurrent !== bCurrent) return aCurrent ? -1 : 1;
        const aExact = a.url === chrome.runtime.getURL("tabs.html");
        const bExact = b.url === chrome.runtime.getURL("tabs.html");
        if (aExact !== bExact) return aExact ? -1 : 1;
        if (a.active !== b.active) return a.active ? -1 : 1;
        return Number(a.id) - Number(b.id);
      });
    const keepId = controls.length ? controls[0].id : null;
    for (const tab of controls) {
      if (tab.id === keepId) {
        if (tab.pinned) {
          try {
            await chrome.tabs.update(tab.id, { pinned: false });
            unpinned++;
          } catch (e) {
            errors++;
          }
        }
        continue;
      }
      try {
        await chrome.tabs.remove(tab.id);
        closed++;
      } catch (e) {
        errors++;
      }
    }
    if (keepId != null) {
      for (const tab of tabs.filter(isBlankStartupTab)) {
        try {
          await chrome.tabs.remove(tab.id);
          closed++;
        } catch (e) {
          errors++;
        }
      }
    }
  } catch (e) {
    errors++;
  }
  if (closed || unpinned || errors) {
    await log("info", `extension control cleanup: ${closed} closed, ${unpinned} unpinned, ${errors} error(s) (${reason})`);
  }
  return { closed, unpinned, errors };
}

export async function browserTabBudgetSnapshot() {
  const pinScraperTabs = await scraperTabsPinned();
  const tabSelection = await scraperTabSelection();
  const { residentIds, activeIds, probeId } = tabSelection;
  const violations = [];
  const counts = {
    extension_control_tabs: 0,
    blank_tabs: 0,
    scraper_tabs: 0,
    resident_scraper_tabs: 0,
    probe_scraper_tabs: 0,
    pinned_scraper_tabs: 0,
    pinned_control_tabs: 0,
    per_platform: {},
  };
  let tabs = [];
  try {
    tabs = (await chrome.tabs.query({})) || [];
  } catch (e) {
    return {
      ok: false,
      violations: [{ kind: "tab_query_failed", detail: String(e && e.message ? e.message : e) }],
      counts,
      pin_scraper_tabs: pinScraperTabs,
      expanded_platform_tabs: false,
      extension_version: extensionVersion(),
    };
  }

  const controls = tabs.filter((tab) => tab && tab.id != null && isExtensionControlTabUrl(tab.url));
  const blanks = tabs.filter(isBlankStartupTab);
  const rows = scraperTabRows(tabs);
  const selection = selectCanonicalScraperTabRows(tabs);
  counts.extension_control_tabs = controls.length;
  counts.blank_tabs = blanks.length;
  counts.scraper_tabs = rows.length;
  for (const tab of controls) {
    if (tab.pinned) counts.pinned_control_tabs++;
  }
  for (const row of rows) {
    const pid = row.platform.id;
    counts.per_platform[pid] = (counts.per_platform[pid] || 0) + 1;
    if (row.tab && row.tab.pinned) counts.pinned_scraper_tabs++;
    if (residentIds.has(pid)) counts.resident_scraper_tabs++;
    if (probeId === pid) counts.probe_scraper_tabs++;
  }

  if (controls.length !== 1) {
    violations.push({
      kind: controls.length === 0 ? "extension_control_tab_missing" : "extension_control_tab_count",
      count: controls.length,
      expected: 1,
    });
  }
  if (selection.duplicateRows.length) {
    violations.push({
      kind: "duplicate_scraper_tabs",
      count: selection.duplicateRows.length,
    });
  }
  for (const p of scraperPlatforms()) {
    const count = counts.per_platform[p.id] || 0;
    const allowed = 1;
    if (count > allowed) {
      violations.push({
        kind: "scraper_tab_budget_exceeded",
        platform: p.id,
        count,
        allowed,
      });
    }
  }
  if (counts.resident_scraper_tabs > BROWSER_RESIDENT_TAB_LIMIT) {
    violations.push({
      kind: "resident_scraper_tab_budget_exceeded",
      count: counts.resident_scraper_tabs,
      allowed: BROWSER_RESIDENT_TAB_LIMIT,
    });
  }
  if (counts.probe_scraper_tabs > 1 || (counts.probe_scraper_tabs && !probeId)) {
    violations.push({ kind: "recovery_probe_tab_budget_exceeded", count: counts.probe_scraper_tabs, allowed: 1 });
  }
  for (const row of rows) {
    if (!activeIds.has(row.platform.id)) {
      violations.push({ kind: "inactive_scraper_tab_open", platform: row.platform.id });
    }
  }
  if (blanks.length) {
    violations.push({ kind: "blank_startup_tabs", count: blanks.length, allowed: 0 });
  }
  if (!pinScraperTabs && counts.pinned_scraper_tabs) {
    violations.push({
      kind: "pinned_scraper_tabs_without_opt_in",
      count: counts.pinned_scraper_tabs,
      storage_key: SCRAPER_TABS_PINNED_KEY,
    });
  }
  if (counts.pinned_control_tabs) {
    violations.push({ kind: "pinned_extension_control_tabs", count: counts.pinned_control_tabs, allowed: 0 });
  }
  return {
    ok: violations.length === 0,
    violations,
    counts,
    active_probe: probeId,
    pin_scraper_tabs: pinScraperTabs,
    expanded_platform_tabs: false,
    extension_version: extensionVersion(),
  };
}

export async function assertBrowserTabBudget(reason) {
  const snapshot = await browserTabBudgetSnapshot();
  if (!snapshot.ok) {
    await log("warn", `browser tab budget violation (${reason || "check"}): ${JSON.stringify(snapshot.violations.slice(0, 8))}`);
  }
  return snapshot;
}

export function shouldNormalizeSingleFeedTab(p, tab, reason) {
  if (!p || !tab || !tab.url) return false;
  // Platforms where a wandering tab (user clicked into a profile, or the SPA
  // routed away from the working feed URL) should snap back to p.url on the
  // next watchdog / startup / install / manual-reload sweep. x is included
  // because failedScript URLs sit in that same "wandered off canonical" bucket;
  // lemon8 is included because the populated /feed/foryou route is the
  // canonical feed; normalize away from lower-yield profile/post subpaths.
  const NORMALIZE_PLATFORMS = new Set(["instagram", "threads", "tiktok", "x", "facebook", "strava", "lemon8"]);
  if (!NORMALIZE_PLATFORMS.has(p.id)) return false;
  try {
    const u = new URL(tab.url);
    if (u.searchParams.has("failedScript")) return true;
  } catch (e) {
    if (/[?&]failedScript(?:=|&|$)/i.test(String(tab.url || ""))) return true;
  }
  const path = _tpath(tab.url);
  const homePath = _tpath(p.url);
  if (path === homePath || path.startsWith(homePath + "/")) return false;
  return /^(manual_extension_reload|startup|installed|watchdog)$/.test(String(reason || ""));
}

// Chrome tab-group awareness — when the user has grouped their social tabs,
// newly-opened recovery / dedup-replacement tabs should join the same group so
// the layout stays clean. chrome.tabs.query already surfaces groupId on every
// tab under the "tabs" permission (no "tabGroups" permission needed for reads
// or for calling chrome.tabs.group({groupId, tabIds})). Verified empirically
// against Chrome 150 with scripts/probe_tabs_group.py.
export async function scraperGroupHint() {
  try {
    const patterns = scraperUrlPatterns();
    if (!patterns.length) return null;
    const tabs = (await chrome.tabs.query({ url: patterns })) || [];
    // Vote by (groupId, windowId) so ties fall on the busiest group in a single
    // window. groupId -1 (TAB_GROUP_ID_NONE) is skipped — un-grouped tabs mean
    // "the user isn't grouping". Tab-group membership is per-window, so we also
    // capture windowId to route the new tab into the correct window.
    const counts = new Map();
    for (const t of tabs) {
      const gid = Number(t.groupId);
      const wid = Number(t.windowId);
      if (!Number.isFinite(gid) || gid < 0 || !Number.isFinite(wid)) continue;
      const key = gid + "|" + wid;
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    if (!counts.size) return null;
    let bestKey = null, bestCount = 0;
    for (const [k, c] of counts.entries()) {
      if (c > bestCount) { bestKey = k; bestCount = c; }
    }
    const [gid, wid] = bestKey.split("|").map(Number);
    return { groupId: gid, windowId: wid };
  } catch (e) { return null; }
}

// Wrap chrome.tabs.create so newly-opened scraper tabs (a) land in the same
// window as the existing social group and (b) immediately join that group.
// Falls back silently to a plain tabs.create if there's no group to join or
// the grouping call fails (e.g. group was deleted mid-call).
export async function createTabInSocialGroup(createOptions, hint) {
  const groupHint = hint === undefined ? await scraperGroupHint() : hint;
  const opts = { ...createOptions };
  if (groupHint && Number.isFinite(groupHint.windowId)) opts.windowId = groupHint.windowId;
  const created = await chrome.tabs.create(opts);
  if (groupHint && Number.isFinite(groupHint.groupId) && created && created.id != null) {
    try { await chrome.tabs.group({ tabIds: [created.id], groupId: groupHint.groupId }); }
    catch (e) { /* group vanished or cross-window mismatch — leave tab where it is */ }
  }
  return created;
}

export async function setScraperTabPinnedState(tab, shouldPin) {
  if (!tab || tab.id == null || tab.pinned === shouldPin) return false;
  try {
    await chrome.tabs.update(tab.id, { pinned: shouldPin });
    return true;
  } catch (e) {
    return false;
  }
}

// Keep only scheduler-selected browser pages resident. The most useful existing
// tab is retained for each selected platform; rotation replaces idle renderers.
export async function ensureScraperTabsOpen(reason, options = {}) {
  const force = !!options.force;
  if ((!(await autoTabsEnabled()) && !force) || _tabsOpInProgress) return;
  _tabsOpInProgress = true;
  let opened = 0, closed = 0, navigated = 0;
  // Compute group hint once per sweep — cheap chrome.tabs.query, and reusing
  // it keeps every newly-opened tab pointed at the same group.
  const groupHint = await scraperGroupHint();
  const pinScraperTabs = await scraperTabsPinned();
  const { activeIds } = await scraperTabSelection();
  try {
    await closeDuplicateExtensionControlTabs(`ensure:${reason || "tabs"}`).catch(() => {});
    for (const p of scraperPlatforms()) {
      const tabs = (await chrome.tabs.query({ url: platformUrlPatterns(p) })) || [];
      if (!activeIds.has(p.id)) {
        for (const tab of tabs) {
          try { await chrome.tabs.remove(tab.id); closed++; } catch (e) {}
        }
        continue;
      }
      if (tabs.length === 0) {
        try { await createTabInSocialGroup({ url: p.url, pinned: pinScraperTabs, active: false }, groupHint); opened++; await _sleep(_humanGap(3000)); } catch (e) {}
        continue;
      }
      const selection = selectCanonicalScraperTabRows(tabs);
      const keep = selection.rows[0] && selection.rows[0].tab ? selection.rows[0].tab : tabs[0];
      try { await setScraperTabPinnedState(keep, pinScraperTabs); } catch (e) {}
      if (shouldNormalizeSingleFeedTab(p, keep, reason)) {
        try { await chrome.tabs.update(keep.id, { url: p.url }); navigated++; await _sleep(_humanGap(3000)); } catch (e) {}
      }
      for (const tab of tabs) {
        if (tab.id === keep.id) continue;
        try { await chrome.tabs.remove(tab.id); closed++; } catch (e) {}
      }
    }
    if (opened || closed || navigated) await log("info", `tabs: +${opened} opened, ${closed} inactive/duplicate tab(s) closed, ${navigated} canonicalized (${reason})`);
    await assertBrowserTabBudget(`ensure:${reason || "tabs"}`).catch(() => {});
  } finally { _tabsOpInProgress = false; }
}

export async function refreshScraperTabs(options = {}) {
  if (_tabsOpInProgress) return { ok: false, reason: "tabs_operation_in_progress", reloaded: 0, errors: 0 };
  if (!options.force && !(await autoTabsEnabled())) return { ok: false, reason: "auto_tabs_disabled", reloaded: 0, errors: 0 };
  _tabsOpInProgress = true;
  const bypassCache = !!options.bypassCache;
  const reason = options.reason || "refresh";
  const requestedGapMs = Number(options.gapMs);
  const gapMs = Number.isFinite(requestedGapMs) && requestedGapMs >= 250 ? requestedGapMs : 8000;
  const pinScraperTabs = await scraperTabsPinned();
  const { activeIds } = await scraperTabSelection();
  let reloaded = 0;
  let errors = 0;
  let pinStateChanged = 0;
  try {
    const controlCleanup = await closeDuplicateExtensionControlTabs(`refresh:${reason}`).catch(() => ({ errors: 1 }));
    errors += Number(controlCleanup && controlCleanup.errors || 0);
    const allTabs = await chrome.tabs.query({ url: scraperUrlPatterns() });
    const selection = selectCanonicalScraperTabRows(allTabs);
    const closed = await closeDuplicateScraperTabRows([
      ...selection.duplicateRows,
      ...selection.rows.filter((row) => !activeIds.has(row.platform.id)),
    ], `refresh:${reason}`);
    errors += closed.errors;
    const tabs = selection.rows.filter((row) => activeIds.has(row.platform.id)).map((row) => row.tab);
    for (const t of tabs || []) {
      try {
        if (await setScraperTabPinnedState(t, pinScraperTabs)) pinStateChanged++;
        await chrome.tabs.reload(t.id, { bypassCache });
        reloaded++;
        await _sleep(_humanGap(gapMs));
      } catch (e) {
        errors++;
      }
    }
    const mode = bypassCache ? "hard-refreshed" : "auto-refreshed";
    await log("info", `${mode} ${tabs ? tabs.length : 0}/${selection.seen} canonical scraper tab(s), closed ${closed.closed}/${selection.skipped} duplicate(s), normalized pin state on ${pinStateChanged}, staggered ${gapMs}ms → loop respawns fresh (${reason})`);
    const tabBudget = await assertBrowserTabBudget(`refresh:${reason}`).catch(() => null);
    return { ok: true, reloaded, errors, tabs: tabs ? tabs.length : 0, totalTabs: selection.seen, skippedDuplicates: selection.skipped, closedDuplicates: closed.closed, pinStateChanged, tabBudget };
  } finally { _tabsOpInProgress = false; }
}
