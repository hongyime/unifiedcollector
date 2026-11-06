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

export async function publishGroupSnapshot(sock: WASocket, sessionName: string): Promise<void> {
    const groups = await sock.groupFetchAllParticipating();
    for (const group of Object.values(groups as Record<string, any>)) {
        if (!group?.id) continue;
        await publishGroupSnapshotForGroup(sock, sessionName, group.id, group);
    }
}

export async function publishGroupSnapshotForGroup(sock: WASocket, sessionName: string, groupId: string, group?: any): Promise<void> {
    const metadata = group || await sock.groupMetadata(groupId);
    await producer.publish('groups.update', {
        chat_jid: metadata.id,
        name: metadata.subject || null,
        subject: metadata.subject || null,
        description: metadata.desc || null,
        desc: metadata.desc || null,
        participant_count: Array.isArray(metadata.participants) ? metadata.participants.length : null,
        participants: (metadata.participants || []).map((member: any) => ({
            id: member.id,
            admin: member.admin || null,
        })),
        action: 'snapshot',
        session_name: sessionName,
    });
}
