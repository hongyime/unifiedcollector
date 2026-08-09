// History sync handler. WhatsApp pushes historical messages via
// 'messaging-history.set' (driven by SYNC_FULL_HISTORY=true at socket creation).
// We normalize + batch them and publish to 'messages.history' (matches the
// consumer's 'messages.#' binding). The consumer unpacks the {messages:[...]}
// batch into individual ingests.
//
// PASSIVE sync (the initial bootstrap blob WhatsApp pushes on link) only goes
// back a limited window (was stuck ~9 days, 0/N chats "complete"). To go DEEPER
// we now ACTIVELY pull older history per chat via Baileys' fetchMessageHistory()
// — an on-demand request for N messages older than the oldest one we hold. The
// results arrive through this same 'messaging-history.set' handler (syncType
// ON_DEMAND), advancing each chat's oldest watermark until we hit the target
// depth (WHATSAPP_MAX_BACKFILL_AGE_DAYS) or WhatsApp has nothing older (exhausted).
//
// Watermarks are persisted to auth_info/history_watermarks.json so progress
// survives restarts.

import { WASocket, WAMessageKey } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { normalizeMessage } from '../utils/normalize';
import pino from 'pino';
import fs from 'fs';
import path from 'path';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });
const WATERMARK_FILE = path.join(process.env.AUTH_STORAGE_PATH || path.join(process.cwd(), 'auth_info'), 'history_watermarks.json');
const yieldToEventLoop = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

type Watermark = {
    oldestTimestamp: number;       // seconds; oldest message we hold for this chat
    messageCount: number;
    isComplete: boolean;           // true = exhausted (no older history) OR hit target depth
    lastSyncTime: number;
    oldestKey?: WAMessageKey;       // key of the oldest held message — needed to request older
    pendingSince?: number;          // ms; set when an on-demand fetch is in flight
    lastRequestedOldest?: number;   // the oldestTimestamp at the moment we last requested
    lastRequestTime?: number;       // ms; for round-robin fairness across chats
    missCount?: number;             // consecutive no-progress fetches (3 = give up)
};
let watermarks: Record<string, Watermark> = {};
let progressInterval: NodeJS.Timeout | null = null;
let backfillInterval: NodeJS.Timeout | null = null;
let unstuckOnce = false;
// The bridge recreates the socket (and re-calls registerHistoryHandler) on every
// reconnect. The long-lived backfill interval must always use the CURRENT socket,
// not the one captured at first registration (a stale socket's late 'close' event
// used to leave the driver wedged). So we track the live socket module-wide and
// the interval reads it each tick.
let currentSock: WASocket | null = null;
let currentCanFetchHistory: (() => boolean) | null = null;

// messageTimestamp may be a number or a Long — coerce to seconds.
function toSec(t: any): number {
    if (t == null) return 0;
    if (typeof t === 'number') return t;
    if (typeof t === 'object' && typeof t.toNumber === 'function') {
        try { return t.toNumber(); } catch { /* fallthrough */ }
    }
    const n = Number(t);
    return Number.isNaN(n) ? 0 : n;
}

function loadWatermarks(): void {
    try {
        if (fs.existsSync(WATERMARK_FILE)) {
            watermarks = JSON.parse(fs.readFileSync(WATERMARK_FILE, 'utf8'));
        }
    } catch (err) {
        logger.error({ err }, 'Failed to load history watermarks');
    }
}

function saveWatermarks(): void {
    try {
        const dir = path.dirname(WATERMARK_FILE);
        if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
        const tmp = WATERMARK_FILE + '.tmp';
        fs.writeFileSync(tmp, JSON.stringify(watermarks, null, 2));
        fs.renameSync(tmp, WATERMARK_FILE);
    } catch (err) {
        logger.error({ err }, 'Failed to save history watermarks');
    }
}

function readyForHistoryFetch(sk: WASocket | null): boolean {
    if (!sk) return false;
    if (currentCanFetchHistory) return currentCanFetchHistory();
    return Boolean((sk as any)?.authState?.creds?.registered);
}

export function registerHistoryHandler(sock: WASocket, canFetchHistory?: () => boolean): void {
    loadWatermarks();
    currentSock = sock;  // always track the live socket (reconnects recreate it)
    currentCanFetchHistory = canFetchHistory || null;

    if (!progressInterval) {
        progressInterval = setInterval(() => {
            const sk = currentSock;
            const readyForHistory =
                Boolean(sk)
                && readyForHistoryFetch(sk);
            let chats = 0;
            let msgs = 0;
            let complete = 0;
            for (const wm of Object.values(watermarks)) {
                chats++;
                msgs += wm.messageCount;
                if (wm.isComplete) complete++;
            }
            if (!readyForHistory && chats === 0 && msgs === 0) return;
            logger.info(`[HistorySync] Progress: ${complete}/${chats} chats complete, ${msgs} total messages`);
        }, 60000);
    }

    // ── On-demand deep backfill driver ─────────────────────────────────────────
    // Periodically request older history for the chat least-recently asked, paced
    // to WHATSAPP_BACKFILL_REQ_PER_MIN. Retires chats that hit the target depth or
    // return nothing older (exhausted) so we don't spin on them forever.
    const deepEnabled = (process.env.WHATSAPP_DEEP_BACKFILL ?? 'true') !== 'false';
    const reqPerMin = Math.max(1, Number(process.env.WHATSAPP_BACKFILL_REQ_PER_MIN || '12'));
    const maxAgeDays = Number(process.env.WHATSAPP_MAX_BACKFILL_AGE_DAYS || '36500');
    const fetchCount = Math.max(1, Number(process.env.WHATSAPP_FETCH_COUNT || '50'));
    const targetOldestSec = Math.floor(Date.now() / 1000) - maxAgeDays * 86400;
    const PENDING_GRACE_MS = 60000;  // no progress within this => chat exhausted

    // One-time recovery: chats that still have a usable anchor and room above the
    // target depth but were flagged complete were wrongly retired by the old
    // stale-socket bug (transient fetch failures looked like "no older history").
    // Un-stick them so they resume; genuinely-exhausted chats just re-mark later.
    if (!unstuckOnce) {
        unstuckOnce = true;
        let revived = 0;
        for (const wm of Object.values(watermarks)) {
            if (wm.isComplete && wm.oldestKey && wm.oldestTimestamp > targetOldestSec) {
                wm.isComplete = false; wm.missCount = 0; wm.pendingSince = undefined; revived++;
            }
        }
        if (revived) logger.info(`[HistorySync] revived ${revived} chat(s) wrongly marked complete`);
    }

    if (deepEnabled && !backfillInterval) {
        backfillInterval = setInterval(async () => {
            const sk = currentSock;
            if (!sk) return;  // no live socket yet
            const readyForHistory = readyForHistoryFetch(sk);
            if (!readyForHistory) {
                // The interval survives reconnects. If the socket is currently
                // unpaired or waiting for a QR scan, do not call
                // fetchMessageHistory(): Baileys throws "Not authenticated",
                // which used to create thousands of retry warnings and could
                // starve the bridge's HTTP endpoints while the user was trying
                // to scan the QR.
                let dirty = false;
                for (const wm of Object.values(watermarks)) {
                    if (wm.pendingSince) {
                        wm.pendingSince = undefined;
                        dirty = true;
                    }
                }
                if (dirty) saveWatermarks();
                return;
            }
            const nowMs = Date.now();
            // Retire chats whose in-flight request made no progress (exhausted), and
            // chats that reached the configured depth.
            let dirty = false;
            for (const wm of Object.values(watermarks)) {
                if (wm.pendingSince && nowMs - wm.pendingSince > PENDING_GRACE_MS) {
                    if (wm.oldestTimestamp >= (wm.lastRequestedOldest ?? Number.POSITIVE_INFINITY)) {
                        // no older data came back — but WhatsApp THROTTLES on-demand
                        // history, so one empty response ≠ exhausted. Only give up after
                        // 3 consecutive misses; the chat keeps its turn until then.
                        wm.missCount = (wm.missCount ?? 0) + 1;
                        if (wm.missCount >= 3) wm.isComplete = true;
                    } else {
                        wm.missCount = 0;  // progress was made
                    }
                    wm.pendingSince = undefined;
                    dirty = true;
                }
                if (!wm.isComplete && wm.oldestTimestamp > 0 && wm.oldestTimestamp <= targetOldestSec) {
                    wm.isComplete = true;  // reached target depth
                    dirty = true;
                }
            }
            // persist progress so completion/anchors survive restarts (the set-handler
            // only saves when fresh history arrives; throttled chats never would).
            if (dirty) saveWatermarks();
            // Pick the eligible chat we asked about least recently (round-robin).
            let pickJid: string | null = null;
            let pickWm: Watermark | null = null;
            for (const [jid, wm] of Object.entries(watermarks)) {
                if (wm.isComplete || !wm.oldestKey || wm.pendingSince) continue;
                if (!pickWm || (wm.lastRequestTime ?? 0) < (pickWm.lastRequestTime ?? 0)) {
                    pickJid = jid; pickWm = wm;
                }
            }
            if (pickJid && pickWm && pickWm.oldestKey) {
                pickWm.lastRequestedOldest = pickWm.oldestTimestamp;
                pickWm.lastRequestTime = nowMs;
                pickWm.pendingSince = nowMs;
                try {
                    await sk.fetchMessageHistory(fetchCount, pickWm.oldestKey, pickWm.oldestTimestamp);
                    logger.info(`[HistorySync] on-demand fetch ${pickJid} (older than ${new Date(pickWm.oldestTimestamp * 1000).toISOString()})`);
                } catch (err) {
                    // transient (socket reconnecting / rate limit) — DON'T mark the
                    // chat exhausted; just clear the in-flight flag and retry later.
                    pickWm.pendingSince = undefined;
                    const message = (err as Error)?.message || '';
                    if (/not authenticated/i.test(message)) {
                        logger.debug({ jid: pickJid }, 'on-demand fetch skipped while unauthenticated');
                    } else {
                        logger.warn({ err: message, jid: pickJid }, 'on-demand fetch failed (will retry)');
                    }
                }
            }
        }, Math.max(1500, Math.floor(60000 / reqPerMin)));
    }

    sock.ev.on('messaging-history.set', async (data: any) => {
        try {
            const messages = data.messages || [];
            if (messages.length === 0) return;

            const syncType = String(data.syncType) === 'INITIAL_BOOTSTRAP' ? 'INITIAL_BOOTSTRAP' : 'ON_DEMAND';

            const grouped = new Map<string, any[]>();
            for (const msg of messages) {
                const jid = msg.key?.remoteJid;
                if (!jid) continue;
                if (!grouped.has(jid)) grouped.set(jid, []);
                grouped.get(jid)!.push(msg);
            }

            for (const [chatJid, chatMsgs] of grouped.entries()) {
                if (!watermarks[chatJid]) {
                    watermarks[chatJid] = { oldestTimestamp: Infinity, messageCount: 0, isComplete: false, lastSyncTime: 0 };
                }

                const batchSize = 100;
                for (let i = 0; i < chatMsgs.length; i += batchSize) {
                    const batch = chatMsgs.slice(i, i + batchSize);
                    const canonical = batch
                        .map((m) => normalizeMessage(m))
                        .filter((m): m is NonNullable<ReturnType<typeof normalizeMessage>> => Boolean(m));

                    for (const _c of canonical) {
                        watermarks[chatJid].messageCount++;  // oldest tracked below (key+ts together)
                    }

                    if (canonical.length > 0) {
                        await producer.publish('messages.history', {
                            sync_type: syncType,
                            session_name: process.env.SESSION_NAME || 'default',
                            messages: canonical,
                        });
                    }
                    await yieldToEventLoop();
                }

                // Track the oldest RAW message key so we can request older history
                // for this chat. Clear the in-flight flag — progress was made.
                let oldestRaw: any = null;
                let oldestRawTs = Number.POSITIVE_INFINITY;
                for (const m of chatMsgs) {
                    const ts = toSec(m.messageTimestamp);
                    if (ts > 0 && ts < oldestRawTs) { oldestRawTs = ts; oldestRaw = m; }
                }
                if (oldestRaw?.key && (!watermarks[chatJid].oldestKey || oldestRawTs < watermarks[chatJid].oldestTimestamp)) {
                    watermarks[chatJid].oldestTimestamp = oldestRawTs;
                    watermarks[chatJid].oldestKey = oldestRaw.key as WAMessageKey;
                    watermarks[chatJid].missCount = 0;  // older history arrived — keep going
                }
                watermarks[chatJid].pendingSince = undefined;

                watermarks[chatJid].lastSyncTime = Date.now();
                // NOTE: data.isLatest from WhatsApp only means "end of this push", not
                // "no older history exists" — so we do NOT mark complete here. The
                // deep-backfill driver decides completeness (target depth or exhausted).
                logger.info(`[HistorySync] ${chatJid}: ${watermarks[chatJid].messageCount} msgs (${syncType})`);
                await yieldToEventLoop();
            }

            saveWatermarks();
        } catch (err) {
            logger.error({ err }, 'Error in messaging-history.set handler');
        }
    });

    // Seed a backfill anchor from LIVE messages. The 414 chats bootstrapped before
    // this driver existed have a watermark but no oldestKey (the old code never
    // stored one), so the driver couldn't request older history for them. A live
    // message gives us a valid (key, timestamp) anchor to start walking backward
    // from — fetchMessageHistory pulls everything OLDER than it (the consumer dedups
    // any overlap with the ~9 days we already hold).
    sock.ev.on('messages.upsert', (ev: any) => {
        try {
            for (const m of ev?.messages || []) {
                const jid = m.key?.remoteJid;
                if (!jid || !m.key) continue;
                const ts = toSec(m.messageTimestamp);
                if (!ts) continue;
                let wm = watermarks[jid];
                if (!wm) {
                    wm = watermarks[jid] = { oldestTimestamp: ts, messageCount: 0, isComplete: false, lastSyncTime: 0, oldestKey: m.key };
                } else if (!wm.oldestKey) {
                    // anchor an existing keyless chat so deep-backfill can begin
                    wm.oldestKey = m.key;
                    wm.oldestTimestamp = ts;
                    wm.isComplete = false;
                }
            }
        } catch (err) {
            logger.debug({ err }, 'anchor-seed from messages.upsert failed');
        }
    });

    sock.ev.on('connection.update', (update) => {
        // Persist on close, but do NOT tear down the module-level intervals here: a
        // stale socket's late 'close' would otherwise kill the freshly-recreated
        // driver after a reconnect (and wedge backfill — the bug this replaces). The
        // intervals are idempotent and always read the current socket; transient
        // disconnects are handled by the per-fetch try/catch.
        if (update.connection === 'close') {
            saveWatermarks();
        }
    });
}

export function getHistoryProgress() {
    let chats = 0;
    let messages = 0;
    let complete = 0;
    let anchored = 0;
    let pending = 0;
    let eligible = 0;
    let oldestTimestamp: number | null = null;
    let newestSyncTime: number | null = null;
    for (const wm of Object.values(watermarks)) {
        chats++;
        messages += wm.messageCount || 0;
        if (wm.isComplete) complete++;
        if (wm.oldestKey) anchored++;
        if (wm.pendingSince) pending++;
        if (!wm.isComplete && wm.oldestKey && !wm.pendingSince) eligible++;
        if (wm.oldestTimestamp && Number.isFinite(wm.oldestTimestamp)) {
            oldestTimestamp = oldestTimestamp == null ? wm.oldestTimestamp : Math.min(oldestTimestamp, wm.oldestTimestamp);
        }
        if (wm.lastSyncTime) {
            newestSyncTime = newestSyncTime == null ? wm.lastSyncTime : Math.max(newestSyncTime, wm.lastSyncTime);
        }
    }
    return {
        chats,
        messages,
        complete,
        anchored,
        pending,
        eligible,
        oldest_message_at: oldestTimestamp ? new Date(oldestTimestamp * 1000).toISOString() : null,
        newest_sync_at: newestSyncTime ? new Date(newestSyncTime).toISOString() : null,
        driver_ready: readyForHistoryFetch(currentSock),
    };
}
