// Native WhatsApp bridge entrypoint for unifiedcollector.
//
// Connects to WhatsApp via Baileys, registers event handlers that publish
// normalized events to the RabbitMQ 'whatsapp.events' topic exchange, and
// exposes a /health endpoint for the Docker healthcheck.
//
// Session auth is loaded from AUTH_STORAGE_PATH (mounted from host
// sessions/whatsapp/<account>). syncFullHistory defaults ON so historical
// messages backfill on first connect (the old bridge left it off -> 0/0 sync).

import makeWASocket, { DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import fs from 'fs';
import express from 'express';
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

let stream515: number[] = [];
const MAX_RAPID_515 = 3;
const WINDOW_515_MS = 60_000;

app.get('/health', (_req, res) => {
    res.status(200).json({ status: 'ok', whatsapp_ready: serviceHealthy });
});
app.get('/ready', (_req, res) => {
    res.status(serviceHealthy ? 200 : 503).json({ status: serviceHealthy ? 'ready' : 'not_ready' });
});
app.listen(port, () => logger.info(`Health server on :${port}`));

const getEnv = (key: string, dflt = ''): string => (process.env[key] || dflt).split('#')[0].trim();

function clearAuthState(): void {
    const authPath = process.env.AUTH_STORAGE_PATH || `./auth_info/${getEnv('SESSION_NAME', 'default')}`;
    try {
        fs.rmSync(authPath, { recursive: true, force: true });
        logger.info({ authPath }, 'Auth state cleared');
    } catch (e) {
        logger.error({ err: e }, 'Failed to clear auth state');
    }
}

async function connectToWhatsApp(): Promise<void> {
    const sessionName = getEnv('SESSION_NAME', 'default');

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
    bindStore(sock);
    registerMessagesHandler(sock);
    registerHistoryHandler(sock);
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
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            logger.info('QR code received -- scan with your phone:');
            qrcode.generate(qr, { small: true });
        }

        if (connection === 'connecting') {
            logger.info('Connecting to WhatsApp...');
            await producer.publish('session.status', { session_name: sessionName, status: 'connecting' }).catch(() => {});
        } else if (connection === 'open') {
            logger.info('Connected to WhatsApp successfully!');
            serviceHealthy = true;
            retryCount = 0;
            if (sock.user?.id) phoneNumber = sock.user.id.split(':')[0];
            await producer.publish('session.status', {
                session_name: sessionName, phone_number: phoneNumber, status: 'active',
            }).catch(() => {});

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

            const error = lastDisconnect?.error as Boom;
            const statusCode = error?.output?.statusCode;
            logger.error({ statusCode, reason: error?.message }, 'Connection closed');
            await producer.publish('session.status', {
                session_name: sessionName, phone_number: phoneNumber, status: 'disconnected',
                details: { statusCode },
            }).catch(() => {});

            if (statusCode === DisconnectReason.loggedOut) {
                logger.error('Logged out -- clearing auth, awaiting QR re-pair');
                clearAuthState();
                setTimeout(() => connectToWhatsApp().catch((e) => logger.error({ err: e }, 'Reconnect failed')), 5000);
            } else if (statusCode === DisconnectReason.badSession) {
                logger.error('Bad session -- clearing auth, reconnecting');
                clearAuthState();
                setTimeout(() => connectToWhatsApp().catch((e) => logger.error({ err: e }, 'Reconnect failed')), 5000);
            } else if (statusCode === DisconnectReason.restartRequired || statusCode === 515) {
                const now = Date.now();
                stream515.push(now);
                stream515 = stream515.filter((t) => now - t < WINDOW_515_MS);
                if (stream515.length >= MAX_RAPID_515) {
                    logger.error(`${stream515.length} stream errors in window -- auth likely corrupt, clearing`);
                    stream515 = [];
                    clearAuthState();
                    setTimeout(() => connectToWhatsApp().catch((e) => logger.error({ err: e }, 'Reconnect failed')), 5000);
                } else {
                    logger.info(`Status ${statusCode}: restart required, reconnecting`);
                    setTimeout(() => connectToWhatsApp().catch((e) => logger.error({ err: e }, 'Reconnect failed')), 500);
                }
            } else if (statusCode === DisconnectReason.connectionReplaced) {
                logger.error('Connection replaced by another session -- stopping');
                process.exit(1);
            } else {
                retryCount++;
                const delay = Math.min(2000 * Math.pow(1.5, retryCount), 60000);
                logger.warn(`Closed (status ${statusCode}); retry ${retryCount} in ${Math.round(delay / 1000)}s`);
                setTimeout(() => connectToWhatsApp().catch((e) => logger.error({ err: e }, 'Reconnect failed')), delay);
            }
        }
    });

    sock.ev.on('creds.update', saveCreds);
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
