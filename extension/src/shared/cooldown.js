// Anti-ban cooldown coordination.
//
// The extension's throttle wall (see shared/throttle.js) is scoped by
// `platform + identity` so that a 429 that hits account A doesn't wall
// account B when the user swaps profiles. This module exposes the
// primitives that turn a live DOM / cookie / page state into a stable
// account identity string, plus a per-platform-cached lookup that avoids
// re-running the DOM probes on every scrape cycle.
//
// Consumer: content.js. Every function here reads only browser globals
// (`document`, `location`, `window`, `localStorage`) — safe to run inside
// any content-script context.

export function instagramLoggedInOwner() {
  try {
    const username =
      window._sharedData &&
      window._sharedData.config &&
      window._sharedData.config.viewer &&
      window._sharedData.config.viewer.username;
    if (username) return String(username).trim().replace(/^@/, "");
  } catch (e) { /* window._sharedData may not exist */ }
  const m = document.cookie.match(/ds_user_id=(\d+)/);
  return m ? m[1] : "";
}

export function facebookLoggedInOwner() {
  try {
    const m = document.cookie.match(/c_user=(\d+)/);
    if (m) return m[1];
  } catch (e) { /* document.cookie may be blocked */ }
  return "";
}

export function xLoggedInOwner() {
  const sources = [
    document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]'),
    document.querySelector('a[data-testid="AppTabBar_Profile_Link"]'),
    ...document.querySelectorAll('a[href^="/"][aria-label*="Profile" i]'),
  ].filter(Boolean);
  for (const el of sources) {
    const txt = el.innerText || el.getAttribute("aria-label") || "";
    const m = txt.match(/@([A-Za-z0-9_]{1,20})/);
    if (m) return m[1];
    const href = el.getAttribute && (el.getAttribute("href") || "");
    const h = href.match(/^\/([A-Za-z0-9_]{1,20})\/?$/);
    if (h && !/^(home|explore|notifications|messages|i|search)$/i.test(h[1])) return h[1];
  }
  return "";
}

export function threadsLoggedInOwner() {
  const sources = [
    ...document.querySelectorAll('a[href^="/@"][aria-label*="Profile" i]'),
    ...document.querySelectorAll('a[href^="/@"]'),
  ];
  for (const el of sources) {
    const txt = el.innerText || el.getAttribute("aria-label") || "";
    const m = txt.match(/@([A-Za-z0-9._]{1,30})/);
    if (m) return m[1];
    const href = el.getAttribute && (el.getAttribute("href") || "");
    const h = href.match(/^\/@([A-Za-z0-9._]{1,30})\/?$/);
    if (h) return h[1];
  }
  return "";
}

/**
 * Read the cached per-platform owner from localStorage, falling back to
 * `domFn()` when the cache is empty. On a fresh DOM hit the value is
 * persisted for next time. Owners are normalized (trimmed, `@` prefix
 * stripped).
 *
 * @param {string} platform
 * @param {() => string} domFn
 * @returns {string}
 */
export function ownerFromStoredOrDom(platform, domFn) {
  const k = "uc_owner_" + platform;
  let owner = "";
  try { owner = (localStorage.getItem(k) || "").trim().replace(/^@/, ""); } catch (e) { /* localStorage blocked */ }
  if (!owner && typeof domFn === "function") {
    try { owner = (domFn() || "").trim().replace(/^@/, ""); } catch (e) { /* domFn errored */ }
  }
  if (owner) {
    try { localStorage.setItem(k, owner); } catch (e) { /* quota / blocked */ }
  }
  return owner || "";
}

/**
 * Resolve the current cooldown identity for a given platform. Instagram and
 * Facebook read their owner ID from a cookie every call. TikTok / X /
 * Threads / Facebook prefer the localStorage-cached value with a DOM
 * fallback. Strava resolves via a caller-provided probe (the caller
 * supplies `extra.strava` because the Strava DOM walk depends on
 * content-script utilities that live outside shared/). Returns "" for
 * unknown platforms.
 *
 * @param {string} platform
 * @param {{ strava?: () => string }} [extra]
 * @returns {string}
 */
export function cooldownIdentity(platform, extra = {}) {
  if (platform === "instagram") return instagramLoggedInOwner();
  if (platform === "tiktok") {
    return ownerFromStoredOrDom("tiktok", () => {
      const m = location.pathname.match(/^\/@([^/?#]+)/);
      return m && m[1] ? m[1] : "";
    });
  }
  if (platform === "x") return ownerFromStoredOrDom("x", xLoggedInOwner);
  if (platform === "threads") return ownerFromStoredOrDom("threads", threadsLoggedInOwner);
  if (platform === "facebook") return ownerFromStoredOrDom("facebook", facebookLoggedInOwner);
  if (platform === "strava" && typeof extra.strava === "function") return extra.strava();
  return "";
}
