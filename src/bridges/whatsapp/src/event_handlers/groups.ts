// Groups handler. Publishes group metadata + participant updates to
// 'groups.update'. Supplementary to message-driven chat upserts (the consumer
// upserts chats from each message's chat_jid), but surfaces group subjects and
// membership for richer chat records.

import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export function registerGroupsHandler(sock: WASocket): void {
    sock.ev.on('groups.update', async (updates) => {
        try {
            for (const u of updates) {
                if (!u.id) continue;
                await producer.publish('groups.update', {
                    chat_jid: u.id,
                    name: u.subject || null,
                    subject: u.subject || null,
                    description: (u as any).desc || null,
                    desc: (u as any).desc || null,
                    participant_count: (u as any).size || null,
                    size: (u as any).size || null,
                });
            }
        } catch (err) {
            logger.error({ err }, 'Error in groups.update handler');
        }
    });

    sock.ev.on('group-participants.update', async (event) => {
        try {
            await producer.publish('groups.update', {
                chat_jid: event.id,
                action: event.action,
                participants: event.participants,
                participant_count: null,
            });
        } catch (err) {
            logger.error({ err }, 'Error in group-participants.update handler');
        }
    });
}
