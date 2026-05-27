import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { normalizeMessage } from '../utils/normalize';
import { getCorrelationForRequest } from '../http_routes';
import pino from 'pino';
import fs from 'fs';
import path from 'path';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

const WATERMARK_FILE = path.join(process.cwd(), 'auth_info', 'history_watermarks.json');

let watermarks: Record<string, { oldestTimestamp: number, messageCount: number, isComplete: boolean, lastSyncTime: number }> = {};
let stalledSyncInterval: NodeJS.Timeout | null = null;

function loadWatermarks() {
    try {
        if (fs.existsSync(WATERMARK_FILE)) {
            const data = fs.readFileSync(WATERMARK_FILE, 'utf8');
            watermarks = JSON.parse(data);
        }
    } catch (err) {
        logger.error({ err }, 'Failed to load history watermarks');
    }
}

function saveWatermarks() {
    try {
        const dir = path.dirname(WATERMARK_FILE);
        if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
        // Atomic: write to .tmp then rename — crash mid-write never corrupts the live file
        const tmp = WATERMARK_FILE + '.tmp';
        fs.writeFileSync(tmp, JSON.stringify(watermarks, null, 2));
        fs.renameSync(tmp, WATERMARK_FILE);
    } catch (err) {
        logger.error({ err }, 'Failed to save history watermarks');
    }
}

export function registerHistoryHandler(sock: WASocket) {
    loadWatermarks();

    // Stalled sync detector
    if (!stalledSyncInterval) {
        stalledSyncInterval = setInterval(() => {
            const now = Date.now();
            let totalChats = 0;
            let totalMsg = 0;
            let completeChats = 0;

            for (const [jid, wm] of Object.entries(watermarks)) {
                totalChats++;
                totalMsg += wm.messageCount;
                if (wm.isComplete) completeChats++;

                // If no sync received for this chat in 30s + it's not marked complete
                // In a real scenario we might need to actively request more history,
                // but WhatsApp Web (Baileys) handles this automatically if syncFullHistory is true.
                if (!wm.isComplete && now - wm.lastSyncTime > 30000) {
                    logger.debug(`[HistorySync] Chat ${jid} stalled. Awaiting more history from WA...`);
                }
            }
            logger.info(`[HistorySync] Aggregate Progress: ${completeChats}/${totalChats} chats complete, ${totalMsg} total messages`);
        }, 60000);
    }

    sock.ev.on('messaging-history.set', async (data) => {
        try {
            const historyEvent: any = data;
            const messages = data.messages || [];
            if (messages.length === 0) return;

            const syncType = String(historyEvent.syncType) === 'INITIAL_BOOTSTRAP' ? 'INITIAL_BOOTSTRAP' : 'ON_DEMAND';
            const requestId = historyEvent.requestId || historyEvent.request_id || historyEvent.requestID || null;
            const correlationId = syncType === 'ON_DEMAND' && requestId ? getCorrelationForRequest(String(requestId)) : null;

            const grouped = new Map<string, typeof messages>();
            for (const msg of messages) {
                const jid = msg.key.remoteJid;
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
                    const canonicalBatch = batch
                        .map(msg => normalizeMessage(msg))
                        .filter((msg): msg is NonNullable<ReturnType<typeof normalizeMessage>> => Boolean(msg));

                    for (const canonical of canonicalBatch) {
                        if (canonical.timestamp != null) {
                            const ts = Number(canonical.timestamp);
                            // Track the oldest timestamp seen for progress reporting
                            if (ts < watermarks[chatJid].oldestTimestamp) {
                                watermarks[chatJid].oldestTimestamp = ts;
                            }
                            watermarks[chatJid].messageCount++;
                        }
                    }

                    if (canonicalBatch.length > 0) {
                        await producer.publish('messages.history', {
                            sync_type: syncType,
                            correlation_id: correlationId,
                            session_name: process.env.SESSION_NAME || 'default',
                            messages: canonicalBatch,
                        });
                    }
                }

                watermarks[chatJid].lastSyncTime = Date.now();
                if (data.isLatest) {
                    watermarks[chatJid].isComplete = true;
                }

                logger.info(`[HistorySync] Chat ${chatJid}: synced ${watermarks[chatJid].messageCount} messages, oldest: ${watermarks[chatJid].oldestTimestamp}`);
            }

            saveWatermarks();

        } catch (err) {
            logger.error({ err }, 'Error in messaging-history.set handler');
        }
    });

    sock.ev.on('connection.update', (update) => {
        if (update.connection === 'close') {
            if (stalledSyncInterval) {
                clearInterval(stalledSyncInterval);
                stalledSyncInterval = null;
            }
            saveWatermarks();
        }
    });
}
