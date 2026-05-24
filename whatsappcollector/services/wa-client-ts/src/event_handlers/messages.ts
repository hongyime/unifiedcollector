import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { normalizeMessage } from '../utils/normalize';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export function registerMessagesHandler(sock: WASocket) {
    sock.ev.on('messages.upsert', async (event) => {
        try {
            for (const msg of event.messages) {
                const canonicalMessage = normalizeMessage(msg);
                if (!canonicalMessage) continue;

                logger.debug({
                    message_id: canonicalMessage.message_id,
                    chat_jid: canonicalMessage.chat_jid,
                    message_type: canonicalMessage.message_type
                }, 'Processed WAMessage');

                if (canonicalMessage.message_type !== 'unknown') {
                    // Extract routing_key from canonicalMessage but don't publish it as part of the payload body if needed
                    const routingKey = canonicalMessage.routing_key;
                    await producer.publish(routingKey, canonicalMessage);
                }
            }
        } catch (err) {
            logger.error({ err }, 'Error in messages.upsert handler');
        }
    });
}
