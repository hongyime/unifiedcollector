// Anti-ban cooldown coordination.
//
// The extension's throttle wall (see shared/throttle.ts) is scoped by
// `platform + identity` so that a 429 that hits account A doesn't wall
// account B when the user swaps profiles. This module exposes the
// primitives that turn a live DOM / cookie / page state into a stable
// account identity string, plus a per-platform-cached lookup that avoids
// re-running the DOM probes on every scrape cycle.
//
// Consumer: content.js. Every function here reads only browser globals
// (`document`, `location`, `window`, `localStorage`) — safe to run inside
// any content-script context.

// A live Instagram page sometimes exposes `_sharedData` for legacy reasons.
interface InstagramSharedData {
  config?: {
    viewer?: {
      username?: string;
    };
  };
}

declare global {
  interface Window {
    _sharedData?: InstagramSharedData;
  }
}

export function instagramLoggedInOwner(): string {
  try {
    const username = window._sharedData?.config?.viewer?.username;
    if (username) return String(username).trim().replace(/^@/, "");
  } catch (e) { /* window._sharedData may not exist */ }
  const m = document.cookie.match(/ds_user_id=(\d+)/);
  return m ? m[1] : "";
}

export function facebookLoggedInOwner(): string {
  try {
    const m = document.cookie.match(/c_user=(\d+)/);
    if (m) return m[1];
  } catch (e) { /* document.cookie may be blocked */ }
  return "";
}

export function xLoggedInOwner(): string {
  const sources: Element[] = [
    document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]'),
    document.querySelector('a[data-testid="AppTabBar_Profile_Link"]'),
    ...document.querySelectorAll('a[href^="/"][aria-label*="Profile" i]'),
  ].filter((el): el is Element => el != null);
  for (const el of sources) {
    const txt = (el as HTMLElement).innerText || el.getAttribute("aria-label") || "";
    const m = txt.match(/@([A-Za-z0-9_]{1,20})/);
    if (m) return m[1];
    const href = (el.getAttribute && el.getAttribute("href")) || "";
    const h = href.match(/^\/([A-Za-z0-9_]{1,20})\/?$/);
    if (h && !/^(home|explore|notifications|messages|i|search)$/i.test(h[1])) return h[1];
  }
  return "";
}

export function threadsLoggedInOwner(): string {
  const sources: Element[] = [
    ...document.querySelectorAll('a[href^="/@"][aria-label*="Profile" i]'),
    ...document.querySelectorAll('a[href^="/@"]'),
  ];
  for (const el of sources) {
    const txt = (el as HTMLElement).innerText || el.getAttribute("aria-label") || "";
    const m = txt.match(/@([A-Za-z0-9._]{1,30})/);
    if (m) return m[1];
    const href = (el.getAttribute && el.getAttribute("href")) || "";
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
 */
export function ownerFromStoredOrDom(platform: string, domFn?: () => string): string {
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

export interface CooldownIdentityExtras {
  strava?: () => string;
}

/**
 * Resolve the current cooldown identity for a given platform. Instagram and
 * Facebook read their owner ID from a cookie every call. TikTok / X /
 * Threads / Facebook prefer the localStorage-cached value with a DOM
 * fallback. Strava resolves via a caller-provided probe (the caller
 * supplies `extra.strava` because the Strava DOM walk depends on
 * content-script utilities that live outside shared/). Returns "" for
 * unknown platforms.
 */
export function cooldownIdentity(platform: string, extra: CooldownIdentityExtras = {}): string {
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
