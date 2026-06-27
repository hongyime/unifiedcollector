// Live message handler. Each incoming WhatsApp message is normalized and
// published with a 'messages.*' routing key so the Python collector's
// 'messages.#' bound queue receives it.

import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { normalizeMessage } from '../utils/normalize';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

function toTs(t: any): number {
    if (typeof t === 'number') return t;
    return t?.low || (typeof t?.toNumber === 'function' ? t.toNumber() : Math.floor(Date.now() / 1000));
}

export function registerMessagesHandler(sock: WASocket): void {
    sock.ev.on('messages.upsert', async (event) => {
        try {
            for (const msg of event.messages) {
                // "Delete for everyone" (revoke) arrives as a protocolMessage of type
                // REVOKE (enum value 0) whose .key points at the deleted message. These
                // used to be dropped (normalize → 'unknown'). Publish a deletion event
                // so the collector can flag the original message + when it died.
                const pm: any = (msg.message as any)?.protocolMessage;
                if (pm && (pm.type === 0 || pm.type === 'REVOKE') && pm.key?.id) {
                    await producer.publish('messages.delete', {
                        deletion: true,
                        revoked_message_id: pm.key.id,
                        chat_jid: msg.key?.remoteJid || pm.key?.remoteJid || '',
                        timestamp: toTs(msg.messageTimestamp),
                        session_name: process.env.SESSION_NAME || 'default',
                        routing_key: 'messages.delete',
                    });
                    continue;
                }
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
