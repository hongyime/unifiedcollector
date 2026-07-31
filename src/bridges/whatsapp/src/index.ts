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
import express from 'express';
import crypto from 'crypto';
import * as qrcode from 'qrcode-terminal';

import { getAuthState, handlePairingCode } from './auth_manager';
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

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });
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
const FRESH_QR_MIN_INTERVAL_MS = Number(process.env.WHATSAPP_FRESH_QR_MIN_INTERVAL_MS || 30_000);
const UNPAIRED_QR_RECONNECT_MS = Number(process.env.WHATSAPP_UNPAIRED_QR_RECONNECT_MS || 5_000);
let reconnectTimer: NodeJS.Timeout | null = null;
let socketEpoch = 0;
let saveCredsTimer: NodeJS.Timeout | null = null;
let saveCredsPending: (() => Promise<void>) | null = null;
const POST_PAIR_515_GRACE_MS = Number(process.env.WHATSAPP_POST_PAIR_515_GRACE_MS || 90_000);

let stream515: number[] = [];
const MAX_RAPID_515 = 3;
const WINDOW_515_MS = 60_000;

function isQrRefsExpired(reason: string | null | undefined): boolean {
    return String(reason || '').toLowerCase().includes('qr refs attempts ended');
}

function bridgeState() {
    const user = activeSock?.user || null;
    const wid: string | null = user?.id || null;
    const phone_number = wid ? wid.split(':')[0].split('@')[0] : null;
    return {
        status: serviceHealthy ? 'ready' : (latestQr ? 'awaiting_scan' : connectionState),
        whatsapp_ready: serviceHealthy,
        connected: serviceHealthy,
        registered: socketRegistered,
        qr_available: Boolean(latestQr),
        last_qr_at: latestQrAt ? new Date(latestQrAt).toISOString() : null,
        session_name: process.env.SESSION_NAME || 'default',
        wid,
        phone_number,
        push_name: user?.name || user?.notify || null,
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
    const registered = Boolean(serviceHealthy || socketRegistered || activeSock?.authState?.creds?.registered);
    if (registered) {
        res.status(200).json({
            ...bridgeState(),
            status: 'registered_session',
            note: 'bridge is already registered; use reconnect to recover or disconnect to unpair',
        });
        return;
    }
    if (!serviceHealthy && latestQr) {
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
        clearAuthState();
        activeSock?.end?.(new Error('manual fresh QR'));
        scheduleReconnect('manual_fresh_qr', 1000);
        res.status(200).json({ status: 'fresh_qr_requested' });
    } catch (err: any) {
        clearAuthState();
        activeSock?.end?.(new Error('manual fresh QR after logout failure'));
        scheduleReconnect('manual_fresh_qr_after_logout_failure', 1000);
        res.status(200).json({ status: 'fresh_qr_requested', warning: err?.message || 'logout failed; local auth cleared' });
    }
});

// POST /media/decrypt — decrypt and stream WhatsApp media bytes back to caller.
// Body: { messageId, mediaKey, directPath, mimetype? }
// Auth: HMAC-SHA256 of JSON body with BRIDGE_SECRET, passed as X-Signature header.
app.use(express.json({ limit: '1mb' }));
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
        logger.warn({ messageId, err: err?.message }, 'media decrypt failed');
        res.status(500).json({ error: err?.message || 'decrypt failed' });
    }
});

const healthServer = app.listen(port, () => logger.info(`Health server on :${port}`));
healthServer.keepAliveTimeout = 1000;
healthServer.headersTimeout = 3000;

const getEnv = (key: string, dflt = ''): string => (process.env[key] || dflt).split('#')[0].trim();

function clearAuthState(): void {
    const authPath = process.env.AUTH_STORAGE_PATH || `./auth_info/${getEnv('SESSION_NAME', 'default')}`;
    try {
        fs.rmSync(authPath, { recursive: true, force: true });
        socketRegistered = false;
        serviceHealthy = false;
        latestQr = null;
        latestQrAt = null;
        connectionState = 'auth_cleared';
        logger.info({ authPath }, 'Auth state cleared');
    } catch (e) {
        logger.error({ err: e }, 'Failed to clear auth state');
    }
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
            latestQr = qr;
            latestQrAt = Date.now();
            socketRegistered = false;
            connectionState = 'awaiting_scan';
            lastDisconnectStatusCode = null;
            lastDisconnectReason = null;
            lastDisconnectAt = null;
            pairingRecoveryUntil = null;
            logger.info({ qr_available: true }, 'QR code refreshed; scan it from the dashboard link page');
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
            logger.error({ statusCode, reason: error?.message }, 'Connection closed');
            await producer.publish('session.status', {
                session_name: sessionName, phone_number: phoneNumber, status: 'disconnected',
                details: { statusCode },
            }).catch(() => {});

            if (statusCode === DisconnectReason.loggedOut) {
                logger.error('Logged out -- clearing auth, awaiting QR re-pair');
                clearAuthState();
                scheduleReconnect('logged_out', 5000);
            } else if (statusCode === DisconnectReason.badSession) {
                logger.error('Bad session -- clearing auth, reconnecting');
                clearAuthState();
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
                        clearAuthState();
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
            } else if (!sock.authState.creds.registered && isQrRefsExpired(error?.message)) {
                retryCount = 0;
                connectionState = 'refreshing_qr';
                const delay = Math.max(1000, UNPAIRED_QR_RECONNECT_MS);
                logger.warn(`QR expired before scan; refreshing QR in ${Math.round(delay / 1000)}s`);
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
connectToWhatsApp().catch((err) => {
    console.error('[FATAL] Failed to start WhatsApp client:', err);
    setTimeout(() => process.exit(1), 500);
});
