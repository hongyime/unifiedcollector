// @ts-nocheck — split out of background.ts (CodeScene hotspot refactor).
// browser_upload: 21 top-level defs moved verbatim; imports added for cross-module refs.
import { withExtensionVersion } from "./heartbeat.js";
import { tabForPlatform } from "./loops.js";
import { bridgeHeaders } from "../shared/ingest_client.js";
import { log } from "../shared/log.js";
import { createTabInSocialGroup } from "./tab_selection.js";

  // don't re-nudge the same tab more often than this
export const BROWSER_UPLOAD_MAX_BYTES = 256 * 1024 * 1024;

export const BROWSER_UPLOAD_FETCH_TIMEOUT_MS = 45000;

export const BROWSER_UPLOAD_POST_TIMEOUT_MS = 65000;

export const BROWSER_UPLOAD_ATTEMPT_LIMIT_BY_PLATFORM = {
  instagram: 4,
  threads: 3,
  tiktok: 2,
  facebook: 2,
  x: 1,
  lemon8: 1,
};

export const browserUploadChains = {};

export function browserUploadAllowed(platform, rawUrl) {
  let host = "";
  try { host = new URL(rawUrl || "").hostname.toLowerCase(); } catch (e) { return false; }
  if (platform === "tiktok") return tiktokBrowserUploadHostAllowed(host);
  const rules = {
    facebook: /(^|\.)(fbcdn\.net)$/i,
    threads: /(^|\.)((fbcdn\.net)|(cdninstagram\.com))$/i,
    instagram: /(^|\.)((fbcdn\.net)|(cdninstagram\.com))$/i,
    x: /(^|\.)((twimg\.com)|(twitter\.com)|(x\.com))$/i,
    lemon8: /(^|\.)((lemon8-app\.com)|(byteimg\.com)|(ibytedtos\.com))$/i,
  };
  return Boolean((rules[platform] || /$a/).test(host));
}

export function tiktokBrowserUploadHostAllowed(host) {
  const h = String(host || "").toLowerCase();
  if (/(^|\.)((tiktokcdn|tiktokv|byteoversea|byteimg|ibytedtos|muscdn)\.com)$/i.test(h)) return true;
  if (h === "sf16-website-login.neutral.ttwstatic.com") return true;
  // TikTok web playback URLs are first-party CDN hosts, not profile/page hosts.
  // Example seen live: v16-webapp-prime.tiktok.com/video/...
  return /^v\d+-webapp(?:-[a-z0-9-]+)?\.tiktok\.com$/i.test(h);
}

export function shouldBrowserUploadMedia(item) {
  if (!item || !item.url) return false;
  if (item.browser_upload === true || item.browser_upload_only === true) return true;
  return String(item.content_type || "").toLowerCase() === "video";
}

export function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(out);
}

export function platformUploadKey(platform) {
  return String(platform || "unknown").toLowerCase() || "unknown";
}

export function browserUploadAttemptLimit(platform) {
  const n = Number(BROWSER_UPLOAD_ATTEMPT_LIMIT_BY_PLATFORM[platformUploadKey(platform)]);
  return Number.isFinite(n) && n > 0 ? n : 2;
}

export async function fetchWithAbort(url, options, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...(options || {}), signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

export async function uploadMediaViaBrowser(base, msg, item) {
  const key = platformUploadKey(msg.platform || "instagram");
  const prev = browserUploadChains[key] || Promise.resolve();
  const next = prev.catch(() => null).then(() => uploadMediaViaBrowserNow(base, msg, item));
  browserUploadChains[key] = next.catch(() => null);
  return next;
}

export function safeUploadFilename(item, blob) {
  const rawKind = String(item && item.content_type ? item.content_type : "media").toLowerCase();
  const rawId = String(item && item.content_id ? item.content_id : "browser_upload");
  const kind = rawKind.replace(/[^a-z0-9_.-]+/g, "_").slice(0, 24) || "media";
  const id = rawId.replace(/[^a-zA-Z0-9_.-]+/g, "_").slice(0, 96) || "browser_upload";
  const mime = String(blob && blob.type ? blob.type : "").toLowerCase();
  const ext = mime.includes("png") ? ".png"
    : mime.includes("webp") ? ".webp"
    : mime.includes("gif") ? ".gif"
    : mime.includes("mp4") ? ".mp4"
    : mime.includes("jpeg") || mime.includes("jpg") ? ".jpg"
    : "";
  return `${kind}_${id}${ext}`;
}

export async function postBrowserUpload(base, payload, blob) {
  const form = new FormData();
  form.append("metadata", JSON.stringify(payload));
  form.append("file", blob, safeUploadFilename(payload.item || {}, blob));
  let r = await fetchWithAbort(base + "/social/ingest-upload-binary", {
    method: "POST",
    headers: { ...(await bridgeHeaders()) },
    body: form,
  }, BROWSER_UPLOAD_POST_TIMEOUT_MS);
  if (r.status !== 404 && r.status !== 405) return r;

  const b64 = arrayBufferToBase64(await blob.arrayBuffer());
  return fetchWithAbort(base + "/social/ingest-upload", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
    body: JSON.stringify({
      ...payload,
      item: {
        ...(payload.item || {}),
        data_b64: b64,
      },
    }),
  }, BROWSER_UPLOAD_POST_TIMEOUT_MS);
}

export async function uploadMediaViaBrowserNow(base, msg, item) {
  const platform = msg.platform || "instagram";
  if (!browserUploadAllowed(platform, item.url)) return { ok: false, reason: "disallowed_host" };
  try {
    const response = await fetchWithAbort(item.url, {
      credentials: "include",
      cache: "force-cache",
    }, BROWSER_UPLOAD_FETCH_TIMEOUT_MS);
    if (!response.ok) return { ok: false, reason: "http_" + response.status };
    const headerSize = Number(response.headers.get("content-length") || 0);
    if (headerSize && headerSize > BROWSER_UPLOAD_MAX_BYTES) return { ok: false, reason: "too_large_header", bytes: headerSize };
    const blob = await response.blob();
    if (blob.size > BROWSER_UPLOAD_MAX_BYTES) return { ok: false, reason: "too_large", bytes: blob.size };
    const payload = withExtensionVersion({
      platform,
      username: msg.username,
      collection_surface: msg.collection_surface || "unknown",
      item: {
        ...item,
        collection_surface: item.collection_surface || msg.collection_surface || "unknown",
        mime_type: blob.type || response.headers.get("content-type") || null,
        meta: { ...(item.meta || {}), browser_upload: true, browser_upload_transport: "multipart" },
      },
      file_size: blob.size,
      mime_type: blob.type || response.headers.get("content-type") || null,
    });
    const r = await postBrowserUpload(base, payload, blob);
    const j = await r.json().catch(() => ({}));
    const accepted = Number(j.accepted ?? j.stored ?? 0);
    const saved = Number(j.saved ?? (j.deduped ? 0 : j.stored || 0));
    const deduped = j.deduped === true ? 1 : 0;
    const rejectStats = j.reject_stats || null;
    const reason = j.reason || topRejectReason(rejectStats) || (r.ok ? "not_accepted" : "http_" + r.status);
    return {
      ok: r.ok && accepted > 0,
      accepted,
      stored: accepted,
      saved,
      deduped,
      reason,
      reject_stats: rejectStats,
    };
  } catch (e) {
    return { ok: false, reason: e && e.name === "AbortError" ? "timeout" : String(e.message || e) };
  }
}

export function topRejectReason(stats) {
  if (!stats || typeof stats !== "object") return "";
  let best = "";
  let bestCount = -1;
  for (const [key, raw] of Object.entries(stats)) {
    const count = Number(raw || 0);
    if (count > bestCount) {
      best = key;
      bestCount = count;
    }
  }
  return best;
}

export async function uploadBrowserMediaCandidates(base, msg, items) {
  const candidates = (items || []).filter(shouldBrowserUploadMedia);
  const limit = browserUploadAttemptLimit(msg.platform || "instagram");
  const activeCandidates = candidates.slice(0, limit);
  const deferredCandidates = candidates.slice(limit);
  let accepted = 0;
  let saved = 0;
  let deduped = 0;
  let attempted = 0;
  const failures = {};
  const failedCandidates = [];
  for (const item of activeCandidates) {
    attempted++;
    const result = await uploadMediaViaBrowser(base, msg, item);
    if (result.ok) {
      accepted += Number(result.accepted || 1);
      saved += Number(result.saved || 0);
      deduped += Number(result.deduped || 0);
    }
    else {
      failures[result.reason || "failed"] = (failures[result.reason || "failed"] || 0) + 1;
      failedCandidates.push({ item, result, ingest_mode: "browser_upload" });
    }
  }
  for (const item of deferredCandidates) {
    const result = {
      ok: false,
      reason: "deferred_upload_budget",
      upload_attempt_limit: limit,
    };
    failures.deferred_upload_budget = (failures.deferred_upload_budget || 0) + 1;
    failedCandidates.push({ item, result, ingest_mode: "browser_upload" });
  }
  if (attempted || deferredCandidates.length) {
    const detail = ` (${saved} new${deduped ? `, ${deduped} duplicate` : ""})`;
    await log(
      accepted ? (Object.keys(failures).length ? "warn" : "info") : "warn",
      `📦 ${msg.platform || "instagram"} · ${msg.username} · browser-upload ${accepted}/${attempted} accepted${detail}${deferredCandidates.length ? `, ${deferredCandidates.length} deferred` : ""}`
    );
    if (Object.keys(failures).length) await log("warn", `browser-upload misses: ${JSON.stringify(failures)}`);
  }
  if (failedCandidates.length) await recordBrowserMediaCandidateResults(base, msg, failedCandidates);
  return { attempted, deferred: deferredCandidates.length, stored: accepted, accepted, saved, deduped, failures };
}

export function hasActiveBrowserMediaRevisit(items) {
  return (items || []).some((it) => {
    const meta = it && typeof it === "object" && it.meta && typeof it.meta === "object" ? it.meta : {};
    return !!(meta.revisit_content_id || meta.revisit_platform);
  });
}

export async function recordBrowserMediaCandidateResults(base, msg, items) {
  try {
    const r = await fetch(base + "/social/browser-media-candidates", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await bridgeHeaders()) },
      body: JSON.stringify(withExtensionVersion({
        platform: msg.platform || "instagram",
        username: msg.username || "unknown",
        items,
      })),
    });
    if (!r.ok) await log("warn", `browser media candidate ledger failed: HTTP ${r.status}`);
  } catch (e) {
    await log("warn", `browser media candidate ledger failed: ${e.message || e}`);
  }
}

// Returns {opened|focused, tabId}. `active` brings the tab to the foreground so
// the user actually SEES it (the old version opened pinned+inactive, which made
// "Open all" look like nothing happened). New tabs join the social group when
// the user has grouped their scraper tabs (see scraperGroupHint).
export async function openOrFocus(p, { active = false } = {}) {
  try {
    const tab = await tabForPlatform(p);
    if (tab) { await chrome.tabs.update(tab.id, { active }); return { id: p.id, focused: true, tabId: tab.id }; }
    const created = await createTabInSocialGroup({ url: p.url, active });
    await log("info", `opened tab: ${p.label}`);
    return { id: p.id, opened: true, tabId: created.id };
  } catch (e) {
    await log("error", `open ${p.label} failed: ${e.message}`);
    return { id: p.id, error: String(e.message || e) };
  }
}
