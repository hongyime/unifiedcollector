// Persistent log ring buffer used by the extension.
//
// Writer: background.js records events here via `log(level, msg)` — it lands
// in chrome.storage.local under `LOG_KEY` and is capped at `LOG_MAX` entries.
// Readers: popup.js reads it for the activity feed; background.js's diagnostics
// message handler slices the tail for /social/browser-heartbeat payloads.
//
// Kept storage-backed on purpose: the MV3 service worker sleeps after ~30s
// idle, so the log has to survive worker respawn to be useful to the operator.

export const LOG_KEY = "ucLog";
export const LOG_MAX = 200;

/**
 * Append one entry to the ring buffer. `console.log` is preserved so the
 * chrome://extensions "Errors" panel keeps its stream even when storage
 * writes race with worker shutdown.
 *
 * @param {"info"|"warn"|"error"|string} level
 * @param {string} msg
 */
export async function log(level, msg) {
  const entry = { t: Date.now(), level, msg };
  try {
    const { [LOG_KEY]: cur = [] } = await chrome.storage.local.get(LOG_KEY);
    cur.push(entry);
    while (cur.length > LOG_MAX) cur.shift();
    await chrome.storage.local.set({ [LOG_KEY]: cur });
  } catch (e) { /* storage may be unavailable during teardown */ }
  console.log(`[UC ${level}] ${msg}`);
}

/**
 * Read the current log ring buffer. Returns `[]` if storage is unavailable
 * or the key has never been written.
 *
 * @returns {Promise<Array<{t: number, level: string, msg: string}>>}
 */
export async function readLog() {
  try {
    const { [LOG_KEY]: cur = [] } = await chrome.storage.local.get(LOG_KEY);
    return Array.isArray(cur) ? cur : [];
  } catch (e) {
    return [];
  }
}

/** Clear the log ring buffer. */
export async function clearLog() {
  try {
    await chrome.storage.local.set({ [LOG_KEY]: [] });
  } catch (e) { /* storage may be unavailable */ }
}
