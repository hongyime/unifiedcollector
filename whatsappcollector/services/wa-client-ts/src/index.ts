import makeWASocket, { DisconnectReason, Browsers, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import fs from 'fs';
import express from 'express';
import { getAuthState, handlePairingCode } from './auth_manager';
import { store, bindStore, getMessage } from './store';
import { producer } from './producer';
import { mountMediaRoutes } from './media_server';
import { registerWaClientRoutes, updateQrStateFromConnection } from './http_routes';
import * as qrcode from 'qrcode-terminal';

// Import all event handlers
import { registerMessagesHandler } from './event_handlers/messages';
import { registerHistoryHandler } from './event_handlers/history';
import { registerContactsHandler } from './event_handlers/contacts';
import { registerGroupsHandler } from './event_handlers/groups';
import { registerCallsHandler } from './event_handlers/calls';
import { findingsHubSender } from './findings_hub';
import { profilePhotoFetcher } from './profile_photo_fetcher';

// ── Global error handlers — log fatal errors before the process dies ──
process.on('uncaughtException', (err) => {
    console.error('[FATAL] Uncaught exception:', err);
    try { pino({ level: 'fatal' }).fatal({ err }, 'Uncaught exception'); } catch (_) { /* best-effort */ }
    process.exit(1);
});
process.on('unhandledRejection', (reason) => {
    console.error('[FATAL] Unhandled rejection:', reason);
    try { pino({ level: 'fatal' }).fatal({ err: reason }, 'Unhandled rejection'); } catch (_) { /* best-effort */ }
});

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });
const app = express();
const port = 3001;

let serviceHealthy = false;
let activeSock: any = null; // Module-level socket reference for graceful shutdown
let routesMounted = false;

// Liveness endpoint for Docker health checks.
// Must stay 200 while process is running so dependent services can boot even
// before WhatsApp pairing is complete.
app.get('/health', (req, res) => {
    res.status(200).json({
        status: 'ok',
        whatsapp_ready: serviceHealthy,
    });
});

// Readiness endpoint for operational diagnostics.
app.get('/ready', (req, res) => {
    if (serviceHealthy) {
        res.status(200).json({ status: 'ready' });
    } else {
        res.status(503).json({ status: 'not_ready' });
    }
});

// Start HTTP server
app.listen(port, () => {
    logger.info(`Health check server running on port ${port}`);
});

let retryCount = 0;
let isFirstConnect = true;
let cachedVersion: [number, number, number] | null = null;

// Track rapid 515 errors to detect corrupted auth state
let streamError515Timestamps: number[] = [];
const MAX_RAPID_515 = 3;
const RAPID_515_WINDOW_MS = 60_000;

function clearAuthState(sessionName: string): void {
    const authPath = process.env.AUTH_STORAGE_PATH || `./auth_info/${sessionName}`;
    try {
        fs.rmSync(authPath, { recursive: true, force: true });
        logger.info({ authPath }, 'Auth state cleared successfully');
    } catch (e) {
        logger.error({ err: e }, 'Failed to clear auth state directory');
    }
}

// Sanitize environment variables
const getEnv = (key: string, defaultValue: string = ''): string => {
    const value = process.env[key] || defaultValue;
    return value.split('#')[0].trim();
};

async function connectToWhatsApp() {
    const sessionName = getEnv('SESSION_NAME', 'default');

    if (isFirstConnect) {
        // Mount media routes on the main Express app (shares port 3001)
        mountMediaRoutes(app);
        if (!routesMounted) {
            registerWaClientRoutes(app, {
                getSocket: () => activeSock,
                getSessionName: () => sessionName,
            });
            routesMounted = true;
        }
        // Initialize producer only once
        await producer.connect();

        process.on('SIGINT', async () => await shutdown(sessionName));
        process.on('SIGTERM', async () => await shutdown(sessionName));

        isFirstConnect = false;
    }

    const { state, saveCreds } = await getAuthState();

    let version: [number, number, number] = [2, 3000, 1017531287]; // Fallback version
    if (!cachedVersion) {
        try {
            const { version: latestVersion, isLatest } = await fetchLatestBaileysVersion();
            cachedVersion = latestVersion;
            logger.info(`Using WhatsApp Web v${latestVersion.join('.')} (latest: ${isLatest})`);
        } catch (e) {
            logger.warn('Failed to fetch latest WhatsApp version, using fallback');
        }
    }
    version = cachedVersion ?? version;

    const pairingCodePhone = getEnv('PAIRING_CODE_PHONE');
    const usePairingCode = Boolean(pairingCodePhone);

    const sock = makeWASocket({
        auth: state,
        version,
        syncFullHistory: getEnv('SYNC_FULL_HISTORY') === 'true',
        markOnlineOnConnect: false,
        connectTimeoutMs: 60_000,
        keepAliveIntervalMs: 30_000,
        retryRequestDelayMs: 500,
        maxMsgRetryCount: 5,
        getMessage: getMessage as any,
        logger,
        browser: ['Windows', 'Chrome', '122.0.6261.112'],
    });

    // Inject socket into modules that need it (avoids fragile global state)
    activeSock = sock;
    profilePhotoFetcher.setSock(sock);
    bindStore(sock);

    // Register handlers
    registerMessagesHandler(sock);
    registerHistoryHandler(sock);
    registerContactsHandler(sock);
    registerGroupsHandler(sock);
    registerCallsHandler(sock);

    if (usePairingCode && !sock.authState.creds.registered) {
        setTimeout(() => handlePairingCode(sock, pairingCodePhone), 3000);
    }

    let heartbeatInterval: NodeJS.Timeout | null = null;
    let uptimeStart = Date.now();
    let phoneNumber = '';

    sock.ev.on('connection.update', async (update: any) => {
        const { connection, lastDisconnect, qr } = update;

        updateQrStateFromConnection(update, sessionName);

        if (qr) {
            logger.info('QR Code received, scan it with your phone:');
            qrcode.generate(qr, { small: true });
        }

        if (connection === 'connecting') {
            logger.info('Connecting to WhatsApp...');
            try {
                await producer.publish('session.status', {
                    session_name: sessionName,
                    status: 'connecting'
                });
            } catch (e) {
                logger.warn('Failed to publish connecting status to broker');
            }
        } else if (connection === 'open') {
            logger.info('Connected to WhatsApp successfully!');
            serviceHealthy = true;
            retryCount = 0;

            if (sock.user && sock.user.id) {
                phoneNumber = sock.user.id.split(':')[0];
            }

            try {
                if (phoneNumber) {
                    await producer.publish('session.status', {
                        session_name: sessionName,
                        phone_number: phoneNumber,
                        status: 'active'
                    });
                }
            } catch (e) {
                logger.warn('Failed to publish active status to broker');
            }

            if (heartbeatInterval) clearInterval(heartbeatInterval);
            heartbeatInterval = setInterval(async () => {
                try {
                    if (phoneNumber) {
                        await producer.publish('session.heartbeat', {
                            session_name: sessionName,
                            phone_number: phoneNumber,
                            uptime: Math.floor((Date.now() - uptimeStart) / 1000)
                        });
                    }
                } catch (e) {
                    logger.debug('Failed to publish heartbeat (broker may be disconnected)');
                }
            }, 30000);

            // Delayed Start Findings Hub feature to avoid race conditions during initial sync
            setTimeout(async () => {
                try {
                    if (connection === 'open') {
                        await findingsHubSender.start(sock);
                    }
                } catch (e) {
                    logger.error({ err: e }, 'Failed to start Findings Hub (delayed)');
                }
            }, 10000);
        } else if (connection === 'close') {
            if (heartbeatInterval) clearInterval(heartbeatInterval);
            serviceHealthy = false;

            const error = lastDisconnect?.error as Boom;
            const statusCode = error?.output?.statusCode;
            const reason = error?.message;

            logger.error({ statusCode, reason }, 'Connection closed');

            try {
                await producer.publish('session.status', {
                    session_name: sessionName,
                    phone_number: phoneNumber,
                    status: 'disconnected',
                    details: { statusCode, reason }
                });
            } catch (e) {
                logger.warn('Failed to publish disconnected status to broker');
            }

            if (statusCode === DisconnectReason.loggedOut) {
                logger.error('Logged out! Clearing auth state and waiting for re-pair via QR code.');
                serviceHealthy = false;
                try {
                    await producer.publish('session.status', {
                        session_name: sessionName,
                        phone_number: phoneNumber,
                        status: 'banned',
                        details: { reason: 'logged_out' }
                    });
                } catch (e) { }
                clearAuthState(sessionName);
                // Reconnect with empty auth — Baileys will show QR code
                setTimeout(() => connectToWhatsApp().catch(err => logger.error({ err }, 'Reconnect failed')), 5000);
                return;
            } else if (statusCode === DisconnectReason.badSession) {
                logger.error('Bad session file! Clearing auth state and reconnecting...');
                serviceHealthy = false;
                clearAuthState(sessionName);
                setTimeout(() => connectToWhatsApp().catch(err => logger.error({ err }, 'Reconnect failed')), 5000);
            } else if (statusCode === DisconnectReason.restartRequired || statusCode === 515) {
                // Track rapid 515 errors — if too many in a short window, auth is corrupted
                const now = Date.now();
                streamError515Timestamps.push(now);
                streamError515Timestamps = streamError515Timestamps.filter(t => now - t < RAPID_515_WINDOW_MS);

                if (streamError515Timestamps.length >= MAX_RAPID_515) {
                    logger.error(`Detected ${streamError515Timestamps.length} stream errors in ${RAPID_515_WINDOW_MS / 1000}s — auth state is likely corrupted. Clearing and re-pairing...`);
                    serviceHealthy = false;
                    streamError515Timestamps = [];
                    clearAuthState(sessionName);
                    setTimeout(() => connectToWhatsApp().catch(err => logger.error({ err }, 'Reconnect failed')), 5000);
                } else {
                    logger.info(`Status ${statusCode}: Restart required, reconnecting immediately...`);
                    setTimeout(() => connectToWhatsApp().catch(err => logger.error({ err }, 'Reconnect failed')), 500);
                }
            } else if (statusCode === DisconnectReason.connectionReplaced) {
                logger.error('Connection replaced! Another session is active. Stopping.');
                serviceHealthy = false;
                process.exit(1);
            } else if (statusCode === DisconnectReason.timedOut) {
                logger.warn('Connection timed out, reconnecting...');
                serviceHealthy = false;
                setTimeout(() => connectToWhatsApp().catch(err => logger.error({ err }, 'Reconnect failed')), 500);
            } else {
                serviceHealthy = false;
                retryCount++;
                const delay = Math.min(2000 * Math.pow(1.5, retryCount), 60000); // Max 60s delay
                logger.warn(`Connection closed with status ${statusCode}. Retrying in ${Math.round(delay / 1000)}s (Attempt ${retryCount})`);
                setTimeout(() => connectToWhatsApp().catch(err => logger.error({ err }, 'Reconnect failed')), delay);
            }
        }
    });

    sock.ev.on('creds.update', saveCreds);
}

async function shutdown(sessionName: string) {
    logger.info('Shutting down gracefully...');
    try {
        profilePhotoFetcher.saveKnownHashes();
        await producer.publish('session.status', {
            session_name: sessionName,
            status: 'disconnected',
            details: { reason: 'shutdown' }
        });
        await producer.flush();
    } catch (e) {
        logger.error({ err: e }, 'Failed to publish final status');
    }
    activeSock?.ws?.close();
    process.exit(0);
}

console.log(`[wa-client-ts] Starting (session=${process.env.SESSION_NAME || 'default'})...`);
connectToWhatsApp().catch((err: any) => {
    console.error('[FATAL] Failed to start WhatsApp client:', err);
    logger.error({ err }, 'Failed to start WhatsApp client');
    // Give pino time to flush before exiting
    setTimeout(() => process.exit(1), 500);
});
