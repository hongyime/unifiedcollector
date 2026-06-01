// Live message handler. Each incoming WhatsApp message is normalized and
// published with a 'messages.*' routing key so the Python collector's
// 'messages.#' bound queue receives it.

import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { normalizeMessage } from '../utils/normalize';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export function registerMessagesHandler(sock: WASocket): void {
    sock.ev.on('messages.upsert', async (event) => {
        try {
            for (const msg of event.messages) {
                const norm = normalizeMessage(msg);
                if (!norm) continue;
                if (norm.message_type === 'unknown') continue;
                await producer.publish(norm.routing_key, norm);
            }
        } catch (err) {
            logger.error({ err }, 'Error in messages.upsert handler');
        }
    });
}
