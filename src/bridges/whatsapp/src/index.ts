// Native WhatsApp bridge entrypoint for unifiedcollector.
//
// Connects to WhatsApp via Baileys, registers event handlers that publish
// normalized events to the RabbitMQ 'whatsapp.events' topic exchange, and
// exposes liveness/readiness endpoints for Docker + dashboard status.
//
// Session auth is loaded from AUTH_STORAGE_PATH (mounted from host
// sessions/whatsapp/<account>). syncFullHistory defaults ON so historical
// messages backfill on first connect (the old bridge left it off -> 0/0 sync).

import makeWASocket, { DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import fs from 'fs';
import path from 'path';
import express from 'express';
import crypto from 'crypto';
import * as qrcode from 'qrcode-terminal';

import { getAuthState, handlePairingCode, requestPairingCode } from './auth_manager';
import { bindStore, getMessage } from './store';
import { producer } from './producer';
import { registerMessagesHandler } from './event_handlers/messages';
import { registerHistoryHandler } from './event_handlers/history';
import { registerContactsHandler } from './event_handlers/contacts';
import { registerGroupsHandler } from './event_handlers/groups';

process.on('uncaughtException', (err) => {
    console.error('[FATAL] Uncaught exception:', err);
    process.exit(1);
});
process.on('unhandledRejection', (reason) => {
    console.error('[FATAL] Unhandled rejection:', reason);
});

function bridgeLogArgText(arg: any): string {
    if (arg == null) return '';
    if (typeof arg === 'string') return arg;
    if (arg instanceof Error) return `${arg.message}\n${arg.stack || ''}`;
    try {
        return JSON.stringify(arg);
    } catch {
        return String(arg);
    }
}

function shouldSuppressExpectedPairingLog(args: any[]): boolean {
    const text = args.map(bridgeLogArgText).join(' ');
    return text.includes('QR refs attempts ended') && text.includes('connection errored');
}

const logger = pino({
    level: process.env.LOG_LEVEL || 'info',
    hooks: {
        logMethod(this: any, args: any[], method: any) {
            if (shouldSuppressExpectedPairingLog(args)) return;
            return method.apply(this, args);
        },
    },
});
const app = express();
const port = 3001;

let serviceHealthy = false;
let activeSock: any = null;
let retryCount = 0;
let isFirstConnect = true;
let cachedVersion: [number, number, number] | null = null;
let latestQr: string | null = null;
let latestQrAt: number | null = null;
let connectionState = 'starting';
let socketRegistered = false;
let lastDisconnectStatusCode: number | null = null;
let lastDisconnectReason: string | null = null;
let lastDisconnectAt: number | null = null;
let pairingRecoveryUntil: number | null = null;
let terminalQrPrinted = false;
let lastFreshQrRequestAt = 0;
let lastQrEndpointAt = 0;
const FRESH_QR_MIN_INTERVAL_MS = Number(process.env.WHATSAPP_FRESH_QR_MIN_INTERVAL_MS || 30_000);
// QR pairing is operator-driven and time-sensitive. If Baileys closes an
// unpaired socket after "QR refs attempts ended", reconnect quickly so the
// dashboard does not sit in a no-QR gap for half a minute.
const UNPAIRED_QR_RECONNECT_MS = Number(process.env.WHATSAPP_UNPAIRED_QR_RECONNECT_MS || 5_000);
const UNPAIRED_QR_IDLE_RECONNECT_MS = Number(process.env.WHATSAPP_UNPAIRED_QR_IDLE_RECONNECT_MS || 60_000);
const QR_VIEW_ACTIVE_MS = Number(process.env.WHATSAPP_QR_VIEW_ACTIVE_MS || 120_000);
const QR_STABILITY_MS = Number(process.env.WHATSAPP_QR_STABILITY_MS || 45_000);
const AUTH_SCAN_FILE_LIMIT = Number(process.env.WHATSAPP_AUTH_SCAN_FILE_LIMIT || 1000);
const STALE_UNREGISTERED_AUTH_FILE_THRESHOLD = Number(process.env.WHATSAPP_STALE_UNREGISTERED_AUTH_FILE_THRESHOLD || 1000);
const AUTH_STATE_CACHE_TTL_MS = Number(process.env.WHATSAPP_AUTH_STATE_CACHE_TTL_MS || 5_000);
let reconnectTimer: NodeJS.Timeout | null = null;
let socketEpoch = 0;
let saveCredsTimer: NodeJS.Timeout | null = null;
let saveCredsPending: (() => Promise<void>) | null = null;
const POST_PAIR_515_GRACE_MS = Number(process.env.WHATSAPP_POST_PAIR_515_GRACE_MS || 90_000);

let stream515: number[] = [];
const MAX_RAPID_515 = 3;
const WINDOW_515_MS = 60_000;
let lastQrLogAt = 0;

type AuthStateSummary = {
    session_name: string;
    auth_path_exists: boolean;
    creds_json_exists: boolean;
    creds_json_size: number;
    creds_json_mtime: string | null;
    auth_file_count: number;
    auth_file_count_capped: boolean;
    auth_non_watermark_file_seen: boolean;
    has_registered_creds: boolean;
    has_recoverable_state: boolean;
    note: string;
};

let cachedAuthStateSummary: AuthStateSummary | null = null;
let cachedAuthStateSummaryAt = 0;
let authStateRefreshTimer: NodeJS.Timeout | null = null;

function isQrRefsExpired(reason: string | null | undefined): boolean {
    return String(reason || '').toLowerCase().includes('qr refs attempts ended');
}

function qrViewerActive(): boolean {
    return Date.now() - lastQrEndpointAt < QR_VIEW_ACTIVE_MS;
}

function nudgePairingIfQrViewerNeedsCode(): void {
    if (serviceHealthy || latestQr || socketRegistered || reconnectTimer) return;
    if (connectionState === 'connecting' || connectionState === 'connecting_unpaired' || connectionState === 'awaiting_scan') {
        return;
    }
    connectionState = 'waiting_for_fresh_qr';
    scheduleReconnect('qr_endpoint_requested', 1000);
}

type MediaDecryptErrorInfo = {
    status: number;
    code: string;
    retryable: boolean;
    message: string;
};

function redactMediaError(message: string): string {
    return String(message || 'decrypt failed')
        .replace(/https:\/\/\S+/gi, '[media-url]')
        .slice(0, 500);
}

function classifyMediaDecryptError(err: any): MediaDecryptErrorInfo {
    const rawMessage = String(err?.message || err || 'decrypt failed');
    const message = redactMediaError(rawMessage);
    const lower = rawMessage.toLowerCase();

    if (
        lower.includes('failed to fetch stream')
        || lower.includes('media not found')
        || lower.includes('not found')
        || lower.includes('gone')
        || lower.includes('404')
        || lower.includes('410')
    ) {
        return { status: 410, code: 'media_unavailable', retryable: false, message };
    }
    if (
        lower.includes('bad mac')
        || lower.includes('invalid media key')
        || lower.includes('decrypt')
        || lower.includes('hkdf')
    ) {
        return { status: 422, code: 'media_decrypt_failed', retryable: false, message };
    }
    if (
        lower.includes('timeout')
        || lower.includes('timed out')
        || lower.includes('econnreset')
        || lower.includes('network')
        || lower.includes('fetch')
    ) {
        return { status: 503, code: 'media_fetch_transient', retryable: true, message };
    }
    return { status: 500, code: 'media_decrypt_error', retryable: true, message };
}

function bridgeState() {
    const user = activeSock?.user || null;
    const wid: string | null = user?.id || null;
    const phone_number = wid ? wid.split(':')[0].split('@')[0] : null;
    const auth_state = cachedAuthStateSummaryForHealth();
    return {
        status: serviceHealthy ? 'ready' : (latestQr ? 'awaiting_scan' : connectionState),
        whatsapp_ready: serviceHealthy,
        connected: serviceHealthy,
        registered: socketRegistered,
        qr_available: Boolean(latestQr),
        needs_scan: !serviceHealthy && !auth_state.has_registered_creds,
        last_qr_at: latestQrAt ? new Date(latestQrAt).toISOString() : null,
        session_name: process.env.SESSION_NAME || 'default',
        wid,
        phone_number,
        push_name: user?.name || user?.notify || null,
        auth_state,
        last_disconnect_status_code: lastDisconnectStatusCode,
        last_disconnect_reason: lastDisconnectReason,
        last_disconnect_at: lastDisconnectAt ? new Date(lastDisconnectAt).toISOString() : null,
        pairing_recovery_until: pairingRecoveryUntil ? new Date(pairingRecoveryUntil).toISOString() : null,
        pairing_recovery_active: Boolean(pairingRecoveryUntil && Date.now() < pairingRecoveryUntil),
    };
}

app.get('/livez', (_req, res) => {
    res.status(200).json({ status: 'alive', session_name: process.env.SESSION_NAME || 'default' });
});
app.get('/health', (_req, res) => {
    res.status(200).json(bridgeState());
});
app.get('/ready', (_req, res) => {
    res.status(serviceHealthy ? 200 : 503).json(bridgeState());
});
app.get('/qr', (_req, res) => {
    lastQrEndpointAt = Date.now();
    nudgePairingIfQrViewerNeedsCode();
    if (serviceHealthy) {
        res.status(200).json({ ...bridgeState(), status: 'already_paired', qr: null, ready: true });
    } else if (latestQr) {
        // Return the raw wa.me QR payload. The dashboard's
        // /whatsapp/qr/{bridge} handler encodes it to a PNG data URI (via
        // Python's `qrcode` library) — keeping image encoding on the
        // dashboard side means the bridge stays a small event forwarder
        // with no extra npm deps.
        res.status(200).json({ ...bridgeState(), status: 'awaiting_scan', qr: latestQr, ready: false });
    } else {
        res.status(202).json({ ...bridgeState(), qr: null, ready: false });
    }
});

// GET /session — return the identity of the WhatsApp account this bridge is
// paired with. Bryan asked for phone + name so the dashboard can label WHICH
// account each bridge represents after a QR scan (previously the dashboard
// only knew "bridge 1" vs "bridge 2"). Fields:
//   connected     — bool, mirrors serviceHealthy (paired + socket open)
//   session_name  — bridge slot label from env (e.g. "session_1")
//   phone_number  — the digits before ':N' in sock.user.id
//   wid           — the full JID (e.g. "6591234567:12@s.whatsapp.net")
//   push_name     — Baileys' cached push name for the owner (WhatsApp
//                    display name if the user set one, else phone number)
// Returns 200 always; `connected=false` when not paired yet.
app.get('/session', (_req, res) => {
    res.status(200).json(bridgeState());
});

// POST /disconnect — unpair THIS device (logout): removes it from the phone's
// linked-devices list and clears local auth. The connection.update handler then
// re-inits into 'awaiting_scan', so a fresh QR is available at /qr. This is the
// per-device "disconnect then re-scan" control the dashboard exposes.
app.post('/disconnect', async (_req, res) => {
    if (!activeSock) { res.status(503).json({ error: 'no active socket' }); return; }
    try {
        await activeSock.logout();
        res.status(200).json({ status: 'logged_out', note: 'unpaired; scan a new QR at /qr to re-link' });
    } catch (err: any) {
        res.status(500).json({ error: err?.message || 'logout failed' });
    }
});

// POST /reconnect — soft reconnect KEEPING creds (no re-scan). Closes the current
// socket; the close handler reconnects with the existing auth. Kicks a
// stuck-but-paired session without unpairing.
app.post('/reconnect', (_req, res) => {
    try {
        connectionState = 'manual_reconnect';
        activeSock?.end?.(new Error('manual reconnect'));
        res.status(200).json({ status: 'reconnecting' });
    } catch (err: any) {
        res.status(500).json({ error: err?.message || 'reconnect failed' });
    }
});

// POST /fresh-qr — force a new pairable QR for an UNREGISTERED slot.
// This must never clear a registered/paired session. The dashboard exposes a
// separate Disconnect control for explicit unpairing; Fresh QR is only a nudge
// for bridge slots that are already in QR-pairing mode.
app.post('/fresh-qr', async (_req, res) => {
    lastQrEndpointAt = Date.now();
    const auth_state = refreshAuthStateSummary();
    const registered = Boolean(
        serviceHealthy
        || socketRegistered
        || activeSock?.authState?.creds?.registered
        || authPathHasRegisteredCreds()
    );
    if (registered) {
        res.status(200).json({
            ...bridgeState(),
            status: 'registered_session',
            note: 'bridge is already registered; use reconnect to recover or disconnect to unpair',
        });
        return;
    }
    if (!serviceHealthy && latestQr && !authStateNeedsCleanPairing(auth_state)) {
        res.status(200).json({ ...bridgeState(), status: 'awaiting_scan', note: 'active QR already available' });
        return;
    }
    const now = Date.now();
    if (now - lastFreshQrRequestAt < FRESH_QR_MIN_INTERVAL_MS) {
        res.status(202).json({ ...bridgeState(), status: 'fresh_qr_recently_requested' });
        return;
    }
    lastFreshQrRequestAt = now;
    connectionState = 'fresh_qr_requested';
    serviceHealthy = false;
    socketRegistered = false;
    latestQr = null;
    latestQrAt = null;
    lastDisconnectStatusCode = null;
    lastDisconnectReason = null;
    lastDisconnectAt = null;
    pairingRecoveryUntil = null;
    try {
        clearAuthState('fresh_qr');
        activeSock?.end?.(new Error('manual fresh QR'));
        scheduleReconnect('manual_fresh_qr', 1000);
        res.status(200).json({ status: 'fresh_qr_requested' });
    } catch (err: any) {
        clearAuthState('fresh_qr_after_logout_failure');
        activeSock?.end?.(new Error('manual fresh QR after logout failure'));
        scheduleReconnect('manual_fresh_qr_after_logout_failure', 1000);
        res.status(200).json({ status: 'fresh_qr_requested', warning: err?.message || 'logout failed; local auth cleared' });
    }
});

// POST /media/decrypt — decrypt and stream WhatsApp media bytes back to caller.
// Body: { messageId, mediaKey, directPath, mimetype? }
// Auth: HMAC-SHA256 of JSON body with BRIDGE_SECRET, passed as X-Signature header.
app.use(express.json({ limit: '1mb' }));

// POST /pairing-code — QR fallback for unregistered slots.
// Body: { phone: "+6591234567" }. Returns the short WhatsApp pairing code for
// phone-side Linked Devices > Link with phone number. The phone number is not
// stored by the bridge; it is used only for this one Baileys request.
app.post('/pairing-code', async (req, res) => {
    const registered = Boolean(
        serviceHealthy
        || socketRegistered
        || activeSock?.authState?.creds?.registered
        || authPathHasRegisteredCreds()
    );
    if (registered) {
        res.status(200).json({
            ...bridgeState(),
            status: 'registered_session',
            note: 'bridge is already registered; use reconnect to recover or disconnect to unpair',
        });
        return;
    }
    if (!activeSock) {
        res.status(503).json({ ...bridgeState(), error: 'no active socket; wait for QR state then retry' });
        return;
    }
    const phone = String(req.body?.phone || '').trim();
    if (!phone) {
        res.status(400).json({ ...bridgeState(), error: 'phone is required in E.164 format' });
        return;
    }
    try {
        lastQrEndpointAt = Date.now();
        connectionState = 'pairing_code_requested';
        const code = await requestPairingCode(activeSock, phone);
        const digits = phone.replace(/[^0-9]/g, '');
        res.status(200).json({
            ...bridgeState(),
            status: 'pairing_code_requested',
            code,
            phone_last4: digits.slice(-4) || null,
        });
    } catch (err: any) {
        logger.warn({ err: err?.message || String(err) }, 'pairing code request failed');
        res.status(400).json({ ...bridgeState(), error: err?.message || 'pairing code request failed' });
    }
});

app.post('/media/decrypt', async (req, res) => {
    const bridgeSecret = process.env.WHATSAPP_MEDIA_BRIDGE_SECRET || process.env.BRIDGE_SECRET || '';
    if (bridgeSecret) {
        const timestamp = req.headers['x-timestamp'] as string || '';
        const signature = req.headers['x-signature'] as string || '';
        const bodyPayload = JSON.stringify(req.body) + timestamp;
        const expectedBody = crypto.createHmac('sha256', bridgeSecret).update(bodyPayload).digest('hex');
        const expectedLegacy = crypto
            .createHmac('sha256', bridgeSecret)
            .update(`${req.body?.messageId || ''}:${timestamp}`)
            .digest('hex');
        if (signature !== expectedBody && signature !== expectedLegacy) {
            res.status(401).json({ error: 'invalid signature' });
            return;
        }
    }
    if (!activeSock) {
        res.status(503).json({ error: 'bridge not connected' });
        return;
    }
    if (!serviceHealthy || !activeSock?.authState?.creds?.registered) {
        res.status(503).json({
            error: 'bridge is not paired; scan a WhatsApp QR before decrypting media',
            code: 'bridge_unpaired',
            retryable: true,
        });
        return;
    }
    const { messageId, mediaKey, directPath, mimetype } = req.body;
    if (!mediaKey || !directPath) {
        res.status(400).json({ error: 'mediaKey and directPath required' });
        return;
    }
    try {
        // Reconstruct a minimal WAMessage-like object that downloadMediaMessage accepts
        const msgType = (mimetype || '').startsWith('image') ? 'imageMessage'
                      : (mimetype || '').startsWith('video') ? 'videoMessage'
                      : (mimetype || '').startsWith('audio') ? 'audioMessage'
                      : (mimetype || '').startsWith('application') ? 'documentMessage'
                      : 'imageMessage';
        const fakeMsg = {
            key: { id: messageId, remoteJid: 'status@broadcast' },
            message: {
                [msgType]: {
                    mediaKey,
                    directPath,
                    mimetype: mimetype || 'application/octet-stream',
                    url: `https://mmg.whatsapp.net${directPath}`,
                }
            }
        };
        const buffer = await downloadMediaMessage(
            fakeMsg as any,
            'buffer',
            {},
            { logger, reuploadRequest: activeSock.updateMediaMessage }
        );
        res.status(200)
           .set('Content-Type', mimetype || 'application/octet-stream')
           .send(buffer);
    } catch (err: any) {
        const classified = classifyMediaDecryptError(err);
        logger.warn(
            {
                messageId,
                statusCode: classified.status,
                code: classified.code,
                retryable: classified.retryable,
                err: classified.message,
            },
            'media decrypt failed'
        );
        res.status(classified.status).json({
            error: classified.message,
            code: classified.code,
            retryable: classified.retryable,
        });
    }
});

const healthServer = app.listen(port, () => logger.info(`Health server on :${port}`));
// The dashboard QR page polls /health and /qr repeatedly while the phone is
// pairing. A very short keep-alive window makes Chromium occasionally reuse a
// socket just as Node closes it, which surfaces as "Failed to fetch" even
// though the bridge is alive.
healthServer.keepAliveTimeout = 15000;
healthServer.headersTimeout = 20000;

const getEnv = (key: string, dflt = ''): string => (process.env[key] || dflt).split('#')[0].trim();

function currentAuthPath(): string {
    return process.env.AUTH_STORAGE_PATH || `./auth_info/${getEnv('SESSION_NAME', 'default')}`;
}

function authPathHasRegisteredCreds(authPath = currentAuthPath()): boolean {
    const credsPath = path.join(authPath, 'creds.json');
    if (!fs.existsSync(credsPath)) return false;
    try {
        const raw = fs.readFileSync(credsPath, 'utf8');
        const parsed = JSON.parse(raw);
        return Boolean(parsed?.registered || parsed?.me || parsed?.account);
    } catch {
        // A creds.json that exists but cannot be parsed is still operator evidence.
        // Preserve it rather than deleting the only local session copy.
        return true;
    }
}

function authPathHasRecoverableState(authPath = currentAuthPath()): boolean {
    if (!fs.existsSync(authPath)) return false;
    if (authPathHasRegisteredCreds(authPath)) return true;
    try {
        const credsPath = path.join(authPath, 'creds.json');
        if (fs.existsSync(credsPath) && fs.statSync(credsPath).size > 0) {
            return true;
        }
    } catch {
        return true;
    }
    return false;
}

function authDirStats(authPath = currentAuthPath()) {
    const out = {
        auth_path_exists: false,
        auth_file_count: 0,
        auth_file_count_capped: false,
        auth_non_watermark_file_seen: false,
    };
    if (!fs.existsSync(authPath)) return out;
    out.auth_path_exists = true;
    let dir: fs.Dir | null = null;
    try {
        dir = fs.opendirSync(authPath);
        while (true) {
            const entry = dir.readSync();
            if (!entry) break;
            if (entry.name === '.' || entry.name === '..') continue;
            out.auth_file_count += 1;
            if (entry.name !== 'history_watermarks.json') {
                out.auth_non_watermark_file_seen = true;
            }
            if (out.auth_file_count >= AUTH_SCAN_FILE_LIMIT) {
                out.auth_file_count_capped = true;
                break;
            }
        }
    } catch {
        throw new Error('auth_dir_unreadable');
    } finally {
        try { dir?.closeSync(); } catch {}
    }
    return out;
}

function authStateSummary(authPath = currentAuthPath()): AuthStateSummary {
    let dirStats = {
        auth_path_exists: false,
        auth_file_count: 0,
        auth_file_count_capped: false,
        auth_non_watermark_file_seen: false,
    };
    const summary = {
        session_name: process.env.SESSION_NAME || 'default',
        auth_path_exists: dirStats.auth_path_exists,
        creds_json_exists: false,
        creds_json_size: 0,
        creds_json_mtime: null as string | null,
        auth_file_count: dirStats.auth_file_count,
        auth_file_count_capped: dirStats.auth_file_count_capped,
        auth_non_watermark_file_seen: dirStats.auth_non_watermark_file_seen,
        has_registered_creds: false,
        has_recoverable_state: false,
        note: 'auth_path_missing',
    };
    try {
        dirStats = authDirStats(authPath);
        summary.auth_path_exists = dirStats.auth_path_exists;
        summary.auth_file_count = dirStats.auth_file_count;
        summary.auth_file_count_capped = dirStats.auth_file_count_capped;
        summary.auth_non_watermark_file_seen = dirStats.auth_non_watermark_file_seen;
        const credsPath = path.join(authPath, 'creds.json');
        summary.creds_json_exists = fs.existsSync(credsPath);
        if (summary.creds_json_exists) {
            const st = fs.statSync(credsPath);
            summary.creds_json_size = st.size;
            summary.creds_json_mtime = st.mtime.toISOString();
        }
        summary.has_registered_creds = authPathHasRegisteredCreds(authPath);
        summary.has_recoverable_state = authPathHasRecoverableState(authPath);
        if (summary.has_registered_creds) {
            summary.note = 'registered_credentials_present';
        } else if (summary.creds_json_exists) {
            summary.note = 'creds_json_present_but_unregistered';
        } else if (authStateNeedsCleanPairing(summary)) {
            summary.note = 'large_auth_cache_without_creds_json';
        } else if (summary.auth_file_count > 0) {
            summary.note = 'auth_files_present_without_creds_json';
        } else if (summary.auth_path_exists) {
            summary.note = 'empty_auth_path';
        }
    } catch (err: any) {
        summary.note = `auth_state_unreadable: ${err?.message || String(err)}`;
    }
    return summary;
}

function refreshAuthStateSummary(): AuthStateSummary {
    cachedAuthStateSummary = authStateSummary();
    cachedAuthStateSummaryAt = Date.now();
    return cachedAuthStateSummary;
}

function cachedAuthStateSummaryForHealth(): AuthStateSummary {
    const now = Date.now();
    if (!cachedAuthStateSummary || now - cachedAuthStateSummaryAt > AUTH_STATE_CACHE_TTL_MS) {
        return refreshAuthStateSummary();
    }
    return cachedAuthStateSummary;
}

function invalidateAuthStateSummaryCache(): void {
    cachedAuthStateSummary = null;
    cachedAuthStateSummaryAt = 0;
}

function startAuthStateCacheRefresh(): void {
    if (authStateRefreshTimer) return;
    refreshAuthStateSummary();
    authStateRefreshTimer = setInterval(() => {
        try {
            refreshAuthStateSummary();
        } catch (err) {
            logger.debug({ err }, 'auth state cache refresh failed');
        }
    }, Math.max(1000, AUTH_STATE_CACHE_TTL_MS));
    authStateRefreshTimer.unref?.();
}

function authStateNeedsCleanPairing(summary = authStateSummary()): boolean {
    return Boolean(
        summary.auth_path_exists
        && !summary.has_registered_creds
        && !summary.creds_json_exists
        && summary.auth_non_watermark_file_seen
        && summary.auth_file_count >= STALE_UNREGISTERED_AUTH_FILE_THRESHOLD
    );
}

function archiveAuthState(authPath: string, reason: string): string | null {
    if (!fs.existsSync(authPath)) return null;
    const stamp = new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14);
    const parent = path.dirname(authPath);
    const base = path.basename(authPath);
    let archivePath = path.join(parent, `${base}.cleared_${stamp}_${reason}`);
    let suffix = 0;
    while (fs.existsSync(archivePath)) {
        suffix += 1;
        archivePath = path.join(parent, `${base}.cleared_${stamp}_${reason}_${suffix}`);
    }
    fs.renameSync(authPath, archivePath);
    fs.mkdirSync(authPath, { recursive: true });
    return archivePath;
}

function clearAuthState(reason = 'manual'): void {
    const authPath = currentAuthPath();
    try {
        const stats = authDirStats(authPath);
        const shouldArchive = stats.auth_path_exists && stats.auth_file_count > 0;
        const archivePath = shouldArchive ? archiveAuthState(authPath, reason) : null;
        if (!shouldArchive) {
            fs.rmSync(authPath, { recursive: true, force: true });
            fs.mkdirSync(authPath, { recursive: true });
        }
        socketRegistered = false;
        serviceHealthy = false;
        latestQr = null;
        latestQrAt = null;
        invalidateAuthStateSummaryCache();
        connectionState = 'auth_cleared';
        refreshAuthStateSummary();
        logger.info(
            { authPath, archived: Boolean(archivePath), archivePath: archivePath ? path.basename(archivePath) : null },
            'Auth state cleared'
        );
    } catch (e) {
        logger.error({ err: e }, 'Failed to clear auth state');
    }
}

function clearStaleUnregisteredAuthIfNeeded(reason: string): void {
    const summary = refreshAuthStateSummary();
    if (!authStateNeedsCleanPairing(summary)) return;
    logger.warn(
        {
            session_name: summary.session_name,
            auth_file_count: summary.auth_file_count,
            auth_file_count_capped: summary.auth_file_count_capped,
            reason,
        },
        'Large unregistered auth cache without creds.json; archiving before QR pairing'
    );
    clearAuthState(reason);
}

function scheduleReconnect(reason: string, delayMs: number): void {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectToWhatsApp().catch((e) => logger.error({ err: e, reason }, 'Reconnect failed'));
    }, delayMs);
}

function scheduleReconnectIfStillUnready(reason: string, delayMs: number, expectedSock: any, expectedEpoch: number): void {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        if (activeSock !== expectedSock || socketEpoch !== expectedEpoch || serviceHealthy) {
            logger.debug({ reason, expectedEpoch, current_epoch: socketEpoch }, 'Skipping stale conditional reconnect');
            return;
        }
        connectToWhatsApp().catch((e) => logger.error({ err: e, reason }, 'Conditional reconnect failed'));
    }, delayMs);
}

function schedulePairingRecovery(reason: string, delayMs: number, expectedSock: any, expectedEpoch: number): void {
    pairingRecoveryUntil = Date.now() + delayMs;
    connectionState = 'pairing_restart';
    logger.info(
        { reason, delay_ms: delayMs },
        'WhatsApp pairing restart required; preserving partial auth and retrying'
    );
    scheduleReconnectIfStillUnready(reason, delayMs, expectedSock, expectedEpoch);
}

function scheduleCredsSave(saveCreds: () => Promise<void>): void {
    saveCredsPending = saveCreds;
    if (saveCredsTimer) return;
    saveCredsTimer = setTimeout(async () => {
        saveCredsTimer = null;
        const pending = saveCredsPending;
        saveCredsPending = null;
        if (!pending) return;
        try {
            await pending();
        } catch (err) {
            logger.warn({ err }, 'Debounced credential save failed');
        }
    }, 750);
}

async function flushCredsSave(): Promise<void> {
    if (saveCredsTimer) {
        clearTimeout(saveCredsTimer);
        saveCredsTimer = null;
    }
    const pending = saveCredsPending;
    saveCredsPending = null;
    if (!pending) return;
    try {
        await pending();
    } catch (err) {
        logger.warn({ err }, 'Credential save flush failed');
    }
}

let lidBackfillDone = false;

async function emitStoredLidMappings(sessionName: string): Promise<void> {
    if (lidBackfillDone) return;
    lidBackfillDone = true;
    const authPath = process.env.AUTH_STORAGE_PATH || `./auth_info/${getEnv('SESSION_NAME', 'default')}`;
    let files: string[];
    try {
        files = fs.readdirSync(authPath);
    } catch (e) {
        logger.warn({ err: e, authPath }, 'lid backfill: could not read auth dir');
        return;
    }
    // Filter lid-mapping files upfront (synchronous dir listing, not file reads).
    const lidFiles = files.filter(f => /^lid-mapping-\d+_reverse\.json$/.test(f));
    let count = 0;
    let errors = 0;
    // Process in parallel batches to avoid blocking the event loop on Docker FS reads.
    const BATCH = 50;
    for (let i = 0; i < lidFiles.length; i += BATCH) {
        const batch = lidFiles.slice(i, i + BATCH);
        const results = await Promise.allSettled(batch.map(async (file) => {
            const lidNum = file.match(/^lid-mapping-(\d+)_reverse\.json$/)![1];
            const raw = JSON.parse(await fs.promises.readFile(`${authPath}/${file}`, 'utf8'));
            const phone = typeof raw === 'string' ? raw : String(raw);
            const lid = `${lidNum}@lid`;
            const jid = `${phone}@s.whatsapp.net`;
            await producer.publish('contacts.update', {
                jid, lid, display_name: null, phone_number: phone, session_name: sessionName,
            });
        }));
        for (const r of results) r.status === 'fulfilled' ? count++ : errors++;
    }
    logger.info({ count, errors, sessionName }, 'lid backfill: emitted stored lid mappings');
}

async function connectToWhatsApp(): Promise<void> {
    const sessionName = getEnv('SESSION_NAME', 'default');
    const epoch = ++socketEpoch;

    if (isFirstConnect) {
        await producer.connect();
        process.on('SIGINT', () => shutdown(sessionName));
        process.on('SIGTERM', () => shutdown(sessionName));
        isFirstConnect = false;
    }

    refreshAuthStateSummary();
    clearStaleUnregisteredAuthIfNeeded('startup_no_creds');
    const { state, saveCreds } = await getAuthState();

    let version: [number, number, number] = [2, 3000, 1017531287];
    if (!cachedVersion) {
        try {
            const { version: latest, isLatest } = await fetchLatestBaileysVersion();
            cachedVersion = latest;
            logger.info(`Using WhatsApp Web v${latest.join('.')} (latest: ${isLatest})`);
        } catch {
            logger.warn('Falling back to pinned WhatsApp version');
        }
    }
    version = cachedVersion ?? version;

    // History backfill ON by default (overridable). This is what populates the
    // tables on first connect; the old bridge defaulted it off => 0/0 sync.
    const syncFullHistory = getEnv('SYNC_FULL_HISTORY', 'true') !== 'false';

    const sock = makeWASocket({
        auth: state,
        version,
        syncFullHistory,
        markOnlineOnConnect: false,
        connectTimeoutMs: 60_000,
        keepAliveIntervalMs: 30_000,
        retryRequestDelayMs: 500,
        maxMsgRetryCount: 5,
        getMessage: getMessage as any,
        logger,
        browser: ['Windows', 'Chrome', '122.0.6261.112'],
    });

    activeSock = sock;
    socketRegistered = Boolean(sock.authState.creds.registered);
    connectionState = socketRegistered ? 'connecting' : 'connecting_unpaired';
    bindStore(sock);
    registerMessagesHandler(sock);
    registerHistoryHandler(sock, () => activeSock === sock && serviceHealthy && Boolean(sock.authState.creds.registered));
    registerContactsHandler(sock);
    registerGroupsHandler(sock);

    const pairingPhone = getEnv('PAIRING_CODE_PHONE');
    if (pairingPhone && !sock.authState.creds.registered) {
        setTimeout(() => handlePairingCode(sock, pairingPhone), 3000);
    }

    let heartbeat: NodeJS.Timeout | null = null;
    const uptimeStart = Date.now();
    let phoneNumber = '';

    sock.ev.on('connection.update', async (update: any) => {
        if (activeSock !== sock || epoch !== socketEpoch) {
            logger.debug({ epoch, current_epoch: socketEpoch }, 'Ignoring stale WhatsApp socket update');
            return;
        }
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            const now = Date.now();
            if (latestQr && latestQrAt && now - latestQrAt < QR_STABILITY_MS) {
                logger.debug(
                    { qr_age_ms: now - latestQrAt, stable_for_ms: QR_STABILITY_MS },
                    'Keeping existing QR stable for easier scanning'
                );
                return;
            }
            latestQr = qr;
            latestQrAt = now;
            socketRegistered = false;
            connectionState = 'awaiting_scan';
            refreshAuthStateSummary();
            lastDisconnectStatusCode = null;
            lastDisconnectReason = null;
            lastDisconnectAt = null;
            pairingRecoveryUntil = null;
            const shouldLogQr = now - lastQrLogAt > 30_000;
            lastQrLogAt = shouldLogQr ? now : lastQrLogAt;
            if (shouldLogQr) {
                logger.info({ qr_available: true }, 'QR code refreshed; scan it from the dashboard link page');
            } else {
                logger.debug({ qr_available: true }, 'QR code refreshed');
            }
            if (getEnv('WHATSAPP_PRINT_TERMINAL_QR', 'false') === 'true') {
                terminalQrPrinted = true;
                qrcode.generate(qr, { small: true });
            }
        }

        if (connection === 'connecting') {
            connectionState = latestQr ? 'awaiting_scan' : (sock.authState.creds.registered ? 'connecting' : 'connecting_unpaired');
            logger.info('Connecting to WhatsApp...');
            await producer.publish('session.status', { session_name: sessionName, status: 'connecting' }).catch(() => {});
        } else if (connection === 'open') {
            logger.info('Connected to WhatsApp successfully!');
            latestQr = null;
            latestQrAt = null;
            terminalQrPrinted = false;
            serviceHealthy = true;
            socketRegistered = true;
            connectionState = 'open';
            invalidateAuthStateSummaryCache();
            refreshAuthStateSummary();
            lastDisconnectStatusCode = null;
            lastDisconnectReason = null;
            lastDisconnectAt = null;
            pairingRecoveryUntil = null;
            retryCount = 0;
            if (sock.user?.id) phoneNumber = sock.user.id.split(':')[0];
            await producer.publish('session.status', {
                session_name: sessionName, phone_number: phoneNumber, status: 'active',
            }).catch(() => {});

            // On reconnection Baileys skips app state sync so contacts.upsert
            // never fires. Force a resync so contacts with their `lid` fields
            // are emitted and the collector can populate whatsapp_lid_map.
            setTimeout(() => {
                sock.resyncAppState(['regular', 'regular_high', 'regular_low', 'critical_block', 'critical_unblock_low'], false)
                    .catch((e: Error) => logger.warn({ err: e?.message }, 'contacts resync failed'));
            }, 3000);

            // Backfill LID→phone mappings from Baileys' on-disk store.
            // Baileys persists lid-mapping-{lid}_reverse.json files whenever
            // it receives LID_MIGRATION_MAPPING_SYNC protocol messages, but
            // never re-emits them as events on reconnection. We read them
            // directly and publish as contacts.update so the collector can
            // seed whatsapp_lid_map without waiting for new live events.
            setTimeout(() => emitStoredLidMappings(sessionName).catch(() => {}), 5000);

            if (heartbeat) clearInterval(heartbeat);
            heartbeat = setInterval(() => {
                producer.publish('session.heartbeat', {
                    session_name: sessionName, phone_number: phoneNumber,
                    uptime: Math.floor((Date.now() - uptimeStart) / 1000),
                }).catch(() => {});
            }, 30000);
        } else if (connection === 'close') {
            if (heartbeat) clearInterval(heartbeat);
            serviceHealthy = false;
            const qrWasRecent = Boolean(latestQrAt && Date.now() - latestQrAt < 120_000);
            await flushCredsSave();
            socketRegistered = Boolean(sock.authState.creds.registered);
            refreshAuthStateSummary();
            // Clear the stale QR — a new one will be emitted by the next
            // connection.update tick if reauth is needed.
            latestQr = null;
            latestQrAt = null;
            terminalQrPrinted = false;

            const error = lastDisconnect?.error as Boom;
            const statusCode = error?.output?.statusCode;
            lastDisconnectStatusCode = typeof statusCode === 'number' ? statusCode : null;
            lastDisconnectReason = error?.message || null;
            lastDisconnectAt = Date.now();
            connectionState = 'disconnected';
            const qrExpiredBeforeScan = !sock.authState.creds.registered && isQrRefsExpired(error?.message);
            if (qrExpiredBeforeScan) {
                logger.info({ statusCode, reason: error?.message }, 'Connection closed while waiting for QR scan');
            } else {
                logger.error({ statusCode, reason: error?.message }, 'Connection closed');
            }
            await producer.publish('session.status', {
                session_name: sessionName, phone_number: phoneNumber, status: 'disconnected',
                details: { statusCode },
            }).catch(() => {});

            if (statusCode === DisconnectReason.loggedOut) {
                logger.error('Logged out -- clearing auth, awaiting QR re-pair');
                clearAuthState('logged_out');
                scheduleReconnect('logged_out', 5000);
            } else if (statusCode === DisconnectReason.badSession) {
                logger.error('Bad session -- clearing auth, reconnecting');
                clearAuthState('bad_session');
                scheduleReconnect('bad_session', 5000);
            } else if (statusCode === DisconnectReason.restartRequired || statusCode === 515) {
                const now = Date.now();
                stream515.push(now);
                stream515 = stream515.filter((t) => now - t < WINDOW_515_MS);
                const hasRegisteredAuth = Boolean(socketRegistered || sock.authState.creds.registered);
                const isLikelyPairingRestart = qrWasRecent && !serviceHealthy;
                if (stream515.length >= MAX_RAPID_515) {
                    stream515 = [];
                    if (hasRegisteredAuth || isLikelyPairingRestart) {
                        logger.error(
                            `${MAX_RAPID_515} stream errors in window -- preserving WhatsApp auth and backing off`
                        );
                        connectionState = isLikelyPairingRestart ? 'pairing_restart_backoff' : 'rapid_515_backoff';
                        pairingRecoveryUntil = Date.now() + 15000;
                        scheduleReconnect(
                            isLikelyPairingRestart ? 'rapid_515_pairing' : 'rapid_515_registered',
                            15000
                        );
                    } else {
                        logger.error(`${MAX_RAPID_515} stream errors in window before registration -- clearing unpaired auth`);
                        clearAuthState('rapid_515_unregistered');
                        scheduleReconnect('rapid_515_unregistered', 5000);
                    }
                } else {
                    if (hasRegisteredAuth && qrWasRecent) {
                        schedulePairingRecovery(
                            'post_pair_restart_required',
                            POST_PAIR_515_GRACE_MS,
                            sock,
                            epoch
                        );
                    } else if (isLikelyPairingRestart) {
                        schedulePairingRecovery(
                            'post_qr_restart_required',
                            Math.max(5000, UNPAIRED_QR_RECONNECT_MS),
                            sock,
                            epoch
                        );
                    } else {
                        logger.info(`Status ${statusCode}: restart required, reconnecting`);
                        scheduleReconnect('restart_required', 500);
                    }
                }
            } else if (statusCode === DisconnectReason.connectionReplaced) {
                logger.error('Connection replaced by another session -- stopping');
                process.exit(1);
            } else if (qrExpiredBeforeScan) {
                retryCount = 0;
                connectionState = 'refreshing_qr';
                const delay = Math.max(
                    1000,
                    qrViewerActive() ? UNPAIRED_QR_RECONNECT_MS : UNPAIRED_QR_IDLE_RECONNECT_MS,
                );
                logger.info(
                    `QR expired before scan; refreshing QR in ${Math.round(delay / 1000)}s`
                    + (qrViewerActive() ? '' : ' (dashboard QR page idle)')
                );
                scheduleReconnect('qr_refs_expired', delay);
            } else {
                retryCount++;
                const delay = Math.min(2000 * Math.pow(1.5, retryCount), 60000);
                logger.warn(`Closed (status ${statusCode}); retry ${retryCount} in ${Math.round(delay / 1000)}s`);
                scheduleReconnect('connection_closed', delay);
            }
        }
    });

    sock.ev.on('creds.update', () => scheduleCredsSave(saveCreds));
}

async function shutdown(sessionName: string): Promise<void> {
    logger.info('Shutting down gracefully...');
    try {
        await producer.publish('session.status', {
            session_name: sessionName, status: 'disconnected', details: { reason: 'shutdown' },
        });
        await producer.flush();
    } catch (e) {
        logger.error({ err: e }, 'Error during shutdown publish');
    }
    activeSock?.ws?.close();
    process.exit(0);
}

console.log(`[wa-bridge] starting (session=${process.env.SESSION_NAME || 'default'})...`);
startAuthStateCacheRefresh();
connectToWhatsApp().catch((err) => {
    console.error('[FATAL] Failed to start WhatsApp client:', err);
    setTimeout(() => process.exit(1), 500);
});
