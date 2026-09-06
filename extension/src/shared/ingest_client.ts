// HTTP client for the local UnifiedCollector /social/* endpoints.
//
// Two bases:
//   INGEST base (default http://127.0.0.1:8765)  — the ig_ingest aiohttp bridge
//   CONTROL base (default http://127.0.0.1:8700) — the dashboard FastAPI
//
// Both are configurable via chrome.storage.local (`ingestBase`, `controlBase`)
// so the operator can point the extension at a remote box during dev.
// A storage.onChanged listener keeps the in-memory cache in sync — writes
// happen from popup.js (Save endpoint button), reads happen from every
// call site in the service worker.
//
// `postJsonWithTimeout` is the canonical POST wrapper: JSON body, hard
// timeout via AbortController, treats non-2xx as an error with a 180-char
// body snippet so operator logs don't drown in a stack trace.

export const DEFAULT_INGEST = "http://127.0.0.1:8765";
export const DEFAULT_CONTROL = "http://127.0.0.1:8700";

let _cachedIngestBase = null;
let _cachedControlBase = null;
let _cacheListenerAttached = false;

function _attachCacheListener() {
  if (_cacheListenerAttached) return;
  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      if (changes.ingestBase) _cachedIngestBase = changes.ingestBase.newValue || DEFAULT_INGEST;
      if (changes.controlBase) _cachedControlBase = changes.controlBase.newValue || DEFAULT_CONTROL;
    });
    _cacheListenerAttached = true;
  } catch (e) { /* addListener may be unavailable in some contexts */ }
}

_attachCacheListener();

export async function ingestBase() {
  if (_cachedIngestBase) return _cachedIngestBase;
  try {
    const { ingestBase } = await chrome.storage.local.get("ingestBase");
    _cachedIngestBase = ingestBase || DEFAULT_INGEST;
  } catch (e) {
    _cachedIngestBase = DEFAULT_INGEST;
  }
  return _cachedIngestBase;
}

export async function controlBase() {
  if (_cachedControlBase) return _cachedControlBase;
  try {
    const { controlBase } = await chrome.storage.local.get("controlBase");
    _cachedControlBase = controlBase || DEFAULT_CONTROL;
  } catch (e) {
    _cachedControlBase = DEFAULT_CONTROL;
  }
  return _cachedControlBase;
}

/**
 * POST a JSON payload with a hard timeout. Non-2xx responses throw with a
 * short body snippet in the message. On success returns
 * `{ response, body }` — body is the raw text (JSON caller-parsed).
 *
 * @param {string} url
 * @param {any} payload
 * @param {number} [timeoutMs]
 * @returns {Promise<{response: Response, body: string}>}
 */
export async function postJsonWithTimeout(url, payload, timeoutMs = 10000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    });
    const body = await r.text().catch(() => "");
    if (!r.ok) throw new Error(`HTTP ${r.status}${body ? ": " + body.slice(0, 180) : ""}`);
    return { response: r, body };
  } finally {
    clearTimeout(timer);
  }
}
