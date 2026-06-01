// History sync handler. WhatsApp pushes historical messages via
// 'messaging-history.set' (driven by SYNC_FULL_HISTORY=true at socket creation).
// We normalize + batch them and publish to 'messages.history' (matches the
// consumer's 'messages.#' binding). The consumer unpacks the {messages:[...]}
// batch into individual ingests.
//
// Watermarks are persisted to auth_info/history_watermarks.json so progress
// survives restarts. The aggregate progress log makes a stalled 0/0 sync
// visible in the logs.

import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { normalizeMessage } from '../utils/normalize';
import pino from 'pino';
import fs from 'fs';
import path from 'path';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });
const WATERMARK_FILE = path.join(process.env.AUTH_STORAGE_PATH || path.join(process.cwd(), 'auth_info'), 'history_watermarks.json');

type Watermark = { oldestTimestamp: number; messageCount: number; isComplete: boolean; lastSyncTime: number };
let watermarks: Record<string, Watermark> = {};
let progressInterval: NodeJS.Timeout | null = null;

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

export function registerHistoryHandler(sock: WASocket): void {
    loadWatermarks();

    if (!progressInterval) {
        progressInterval = setInterval(() => {
            let chats = 0;
            let msgs = 0;
            let complete = 0;
            for (const wm of Object.values(watermarks)) {
                chats++;
                msgs += wm.messageCount;
                if (wm.isComplete) complete++;
            }
            logger.info(`[HistorySync] Progress: ${complete}/${chats} chats complete, ${msgs} total messages`);
        }, 60000);
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

                    for (const c of canonical) {
                        const ts = Number(c.timestamp);
                        if (!Number.isNaN(ts) && ts < watermarks[chatJid].oldestTimestamp) {
                            watermarks[chatJid].oldestTimestamp = ts;
                        }
                        watermarks[chatJid].messageCount++;
                    }

                    if (canonical.length > 0) {
                        await producer.publish('messages.history', {
                            sync_type: syncType,
                            session_name: process.env.SESSION_NAME || 'default',
                            messages: canonical,
                        });
                    }
                }

                watermarks[chatJid].lastSyncTime = Date.now();
                if (data.isLatest) watermarks[chatJid].isComplete = true;
                logger.info(`[HistorySync] ${chatJid}: ${watermarks[chatJid].messageCount} msgs`);
            }

            saveWatermarks();
        } catch (err) {
            logger.error({ err }, 'Error in messaging-history.set handler');
        }
    });

    sock.ev.on('connection.update', (update) => {
        if (update.connection === 'close') {
            if (progressInterval) {
                clearInterval(progressInterval);
                progressInterval = null;
            }
            saveWatermarks();
        }
    });
}
