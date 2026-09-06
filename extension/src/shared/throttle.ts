// Persistent per-platform throttle wall — anti-ban primitive shared by the
// content-script scrape loops.
//
// A "wall" is a timestamp (Date.now() + N minutes) stored in localStorage
// under `uc_wall_<platform>_<identity>` (or the legacy platform-only key).
// While Date.now() < wall, the content script pauses that platform's
// scrape cycle. Walls survive tab reloads and full browser restarts.
//
// Storage helpers now come from shared/storage_helpers.ts (extracted in
// step 12 of docs/plans/extension-bundler.md). The `cooldownIdentity` and
// runtime-message `send` dependencies remain injected via `initThrottle`
// because they still live in content.js — the cooldown module exposes
// `cooldownIdentity`, but Strava's identity walk depends on a
// content-script utility, so content.js supplies a bound closure.

import { lsBoundedInt, lsGet, lsNum, lsSet } from "./storage_helpers.js";

// Chrome extension API globals — full typings would pull in @types/chrome
// (~2 MiB dep). The pilot uses `any` here so the module compiles cleanly
// under strict mode; step 15 files (background/content/inject/popup/tabs)
// will keep `// @ts-nocheck` initially per the plan.
declare const chrome: any;

/** Per-platform backoff (in minutes) applied on a soft 429 or throttle signal. */
export interface ThrottleBackoffTable {
  readonly instagram: number;
  readonly threads: number;
  readonly x: number;
  readonly tiktok: number;
  readonly facebook: number;
  readonly lemon8: number;
  readonly default: number;
  readonly [platform: string]: number;
}

export const DEFAULT_THROTTLE_BACKOFF_MINS: ThrottleBackoffTable = {
  instagram: 75,
  threads: 20,
  x: 40,
  tiktok: 30,
  facebook: 30,
  lemon8: 30,
  default: 35,
};

export interface ThrottleDeps {
  /** Returns the current per-platform account identity, or "" if unknown. */
  cooldownIdentity: (platform: string) => string;
  /** Runtime-message send client (matches content.js's send()). */
  send: (msg: object) => Promise<unknown>;
}

let _deps: ThrottleDeps | null = null;

/**
 * Provide the platform → account resolver and the runtime-message send
 * client that the throttle primitives call into. Must be invoked before
 * any of the exported functions are used.
 */
export function initThrottle(deps: ThrottleDeps): void {
  _deps = deps;
}

function _requireDeps(): ThrottleDeps {
  if (!_deps) {
    throw new Error(
      "shared/throttle: initThrottle({ cooldownIdentity, send }) must be called before use.",
    );
  }
  return _deps;
}

export function wallKey(platform: string, identity?: string | null): string {
  const deps = _requireDeps();
  const raw = identity || (deps.cooldownIdentity && deps.cooldownIdentity(platform)) || "global";
  const ident = String(raw)
    .trim()
    .replace(/^@/, "")
    .replace(/[^A-Za-z0-9_.-]/g, "_")
    .slice(0, 80) || "global";
  return "uc_wall_" + platform + "_" + ident;
}

export function wallLeftMs(platform: string, identity?: string | null): number {
  const keyed = lsNum(wallKey(platform, identity));
  const legacy = lsNum("uc_wall_" + platform);
  return Math.max(0, Math.max(keyed, legacy) - Date.now());
}

export function setWall(platform: string, mins: number, identity?: string | null): void {
  lsSet(wallKey(platform, identity), String(Date.now() + mins * 60000));
}

// Config-driven throttle walls. Override from DevTools / options with:
//   chrome.storage.local.set({ ucThrottleBackoffMins: { x: 12, threads: 12 } })
// Instagram stays deliberately cautious at 75m by default; shortening it
// aggressively re-extends the account/IP throttle window and raises ban risk.
async function throttleBackoffMins(platform: string, fallback: number = DEFAULT_THROTTLE_BACKOFF_MINS.default): Promise<number> {
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

export async function applyThrottleWall(platform: string, reason: string): Promise<number> {
  const deps = _requireDeps();
  const mins = await throttleBackoffMins(platform);
  const wallMins = Math.max(1, Math.round(mins * (0.85 + Math.random() * 0.45)));
  const identity = deps.cooldownIdentity(platform);
  setWall(platform, wallMins, identity);
  await deps.send({ type: "wall", platform, mins: wallMins, account: identity || null, reason }).catch(() => {});
  return wallMins;
}
