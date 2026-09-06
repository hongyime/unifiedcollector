// Persistent per-platform throttle wall — anti-ban primitive shared by the
// content-script scrape loops.
//
// A "wall" is a timestamp (Date.now() + N minutes) stored in localStorage
// under `uc_wall_<platform>_<identity>` (or the legacy platform-only key).
// While Date.now() < wall, the content script pauses that platform's
// scrape cycle. Walls survive tab reloads and full browser restarts.
//
// Storage helpers now come from shared/storage_helpers.js (extracted in
// step 12 of docs/plans/extension-bundler.md). The `cooldownIdentity` and
// runtime-message `send` dependencies remain injected via `initThrottle`
// because they still live in content.js — the cooldown module exposes
// `cooldownIdentity`, but Strava's identity walk depends on a
// content-script utility, so content.js supplies a bound closure.

import { lsBoundedInt, lsGet, lsNum, lsSet } from "./storage_helpers.js";

export const DEFAULT_THROTTLE_BACKOFF_MINS = {
  instagram: 75,
  threads: 20,
  x: 40,
  tiktok: 30,
  facebook: 30,
  lemon8: 30,
  default: 35,
};

let _deps = null;

/**
 * Provide the platform → account resolver and the runtime-message send
 * client that the throttle primitives call into. Must be invoked before
 * any of the exported functions are used.
 *
 * @param {{
 *   cooldownIdentity: (platform: string) => string,
 *   send: (msg: object) => Promise<any>,
 * }} deps
 */
export function initThrottle(deps) {
  _deps = deps;
}

function _requireDeps() {
  if (!_deps) {
    throw new Error(
      "shared/throttle: initThrottle({ cooldownIdentity, send }) must be called before use.",
    );
  }
  return _deps;
}

export function wallKey(platform, identity) {
  const deps = _requireDeps();
  const raw = identity || (deps.cooldownIdentity && deps.cooldownIdentity(platform)) || "global";
  const ident = String(raw)
    .trim()
    .replace(/^@/, "")
    .replace(/[^A-Za-z0-9_.-]/g, "_")
    .slice(0, 80) || "global";
  return "uc_wall_" + platform + "_" + ident;
}

export function wallLeftMs(platform, identity) {
  const keyed = lsNum(wallKey(platform, identity));
  const legacy = lsNum("uc_wall_" + platform);
  return Math.max(0, Math.max(keyed, legacy) - Date.now());
}

export function setWall(platform, mins, identity) {
  lsSet(wallKey(platform, identity), String(Date.now() + mins * 60000));
}

// Config-driven throttle walls. Override from DevTools / options with:
//   chrome.storage.local.set({ ucThrottleBackoffMins: { x: 12, threads: 12 } })
// Instagram stays deliberately cautious at 75m by default; shortening it
// aggressively re-extends the account/IP throttle window and raises ban risk.
async function throttleBackoffMins(platform, fallback = DEFAULT_THROTTLE_BACKOFF_MINS.default) {
  if (platform === "instagram" && lsGet("ucIg429CooldownMinutes", "") !== "") {
    return lsBoundedInt("ucIg429CooldownMinutes", DEFAULT_THROTTLE_BACKOFF_MINS.instagram, 45, 180);
  }
  try {
    const { ucThrottleBackoffMins = {} } = await chrome.storage.local.get("ucThrottleBackoffMins");
    const raw = ucThrottleBackoffMins[platform] ?? ucThrottleBackoffMins.default;
    const n = Number(raw);
    if (Number.isFinite(n) && n >= 1) return Math.round(n);
  } catch (e) { /* fall through */ }
  return DEFAULT_THROTTLE_BACKOFF_MINS[platform] || fallback;
}

export async function applyThrottleWall(platform, reason) {
  const deps = _requireDeps();
  const mins = await throttleBackoffMins(platform);
  const wallMins = Math.max(1, Math.round(mins * (0.85 + Math.random() * 0.45)));
  const identity = deps.cooldownIdentity(platform);
  setWall(platform, wallMins, identity);
  await deps.send({ type: "wall", platform, mins: wallMins, account: identity || null, reason }).catch(() => {});
  return wallMins;
}
