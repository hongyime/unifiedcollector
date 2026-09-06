// Thin, defensive wrappers around content-script `localStorage`.
//
// content.js originally defined these inline (`const lsGet = (k, d) => ...`)
// as arrow-function consts. Every shared module below (throttle, cooldown)
// re-implemented the same pattern. Extracting them here gives one place to
// audit the try/catch handling that keeps the scraper alive on origins where
// storage is blocked (private-mode Facebook, some Instagram edge shells).

/** Get a string from localStorage, returning `d` if absent or unreadable. */
export const lsGet = (k, d) => {
  try {
    const v = localStorage.getItem(k);
    return v == null ? d : v;
  } catch (e) {
    return d;
  }
};

/** Set a string in localStorage, silently no-op on quota / block. */
export const lsSet = (k, v) => {
  try {
    localStorage.setItem(k, v);
  } catch (e) { /* quota / blocked */ }
};

/** Parse an integer stored under `k`, defaulting to 0. */
export const lsNum = (k) => {
  const n = parseInt(lsGet(k, "0"), 10);
  return Number.isFinite(n) ? n : 0;
};

/** Increment the integer stored under `k` by 1 and return the new value. */
export const lsBump = (k) => {
  const n = lsNum(k) + 1;
  lsSet(k, String(n));
  return n;
};

/**
 * Read an integer from localStorage, clamping to `[min, max]` and falling
 * back to `fallback` when the key is missing or unparsable.
 */
export function lsBoundedInt(key, fallback, min, max) {
  const raw = lsGet(key, "");
  const n = raw === "" ? fallback : parseInt(raw, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}
