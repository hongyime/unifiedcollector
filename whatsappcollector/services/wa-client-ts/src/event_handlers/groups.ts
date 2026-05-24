import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { profilePhotoFetcher } from '../profile_photo_fetcher';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export function registerGroupsHandler(sock: WASocket) {
    sock.ev.on('groups.update', async (groups) => {
        try {
            for (const group of groups) {
                await processGroup(group, sock);
            }
        } catch (err) {
            logger.error({ err }, 'Error in groups.update handler');
        }
    });

    sock.ev.on('groups.upsert', async (groups) => {
        try {
            for (const group of groups) {
                await processGroup(group, sock);
            }
        } catch (err) {
            logger.error({ err }, 'Error in groups.upsert handler');
        }
    });

    sock.ev.on('group-participants.update', async (update) => {
        try {
            await producer.publish('groups.participants.update', update);
        } catch (err) {
            logger.error({ err }, 'Error in group-participants.update handler');
        }
    });
}

async function processGroup(group: any, sock: WASocket) {
    const groupJid = group.id || '';
    const isNewsletter = typeof groupJid === 'string' && groupJid.endsWith('@newsletter');
    const isCommunity = group.isCommunity || !!group.linkedParent;

    const payload = {
        jid: groupJid,
        subject: group.subject,
        description: group.desc || '',
        creator_jid: group.owner || null,
        creation_timestamp: group.creation || null,
        chat_type: isNewsletter ? 'channel' : (isCommunity ? 'community' : 'group'),
        is_community: isCommunity,
        linked_parent: group.linkedParent || null
    };

    await producer.publish('groups.update', payload);

    if (isCommunity && !isNewsletter && typeof sock.groupMetadata === 'function') {
        try {
            const metadata = await sock.groupMetadata(groupJid);
            // Publish participants for the community group itself
            if (metadata && metadata.participants) {
                await producer.publish('groups.participants.update', {
                    id: groupJid,
                    participants: metadata.participants.map((p: any) => p.id),
                    action: 'add'
                });
            }
            // Traverse linked subgroups
            if (metadata && (metadata as any).linkedGroupJids) {
                for (const subJid of (metadata as any).linkedGroupJids) {
                    try {
                        const subMeta = await sock.groupMetadata(subJid);
                        if (subMeta) {
                            await producer.publish('groups.update', {
                                jid: subMeta.id,
                                subject: subMeta.subject,
                                description: subMeta.desc || '',
                                creator_jid: subMeta.owner || null,
                                creation_timestamp: subMeta.creation || null,
                                chat_type: 'group',
                                is_community: false,
                                linked_parent: groupJid
                            });
                        }
                    } catch (subErr) {
                        logger.debug({ jid: subJid }, 'Could not fetch subgroup metadata');
                    }
                    // Rate limit subgroup fetches to avoid 429
                    const subJitter = Math.floor(Math.random() * 1000);
                    await new Promise(r => setTimeout(r, 5000 + subJitter));
                }
            }
        } catch (err) {
            logger.warn({ err, jid: group.id }, 'Failed to traverse community sub-groups');
        }
    }

    try {
        if (!isNewsletter && typeof sock.groupMetadata === 'function') {
            // Rate limit to avoid 429 from WhatsApp
            const metaJitter = Math.floor(Math.random() * 2000);
            await new Promise(r => setTimeout(r, 3000 + metaJitter));
            const metadata = await sock.groupMetadata(groupJid);
            if (metadata && metadata.participants) {
                // Publish participant data for tracking
                if (!isCommunity) {
                    await producer.publish('groups.participants.update', {
                        id: groupJid,
                        participants: metadata.participants.map((p: any) => p.id),
                        action: 'add'
                    });
                }
                // Enqueue profile photo fetches
                for (const p of metadata.participants) {
                    if (p.id && !p.id.includes('@lid')) {
                        profilePhotoFetcher.enqueue(p.id);
                    }
                }
            }
        } else if (isNewsletter) {
            logger.debug({ jid: groupJid }, 'Skipping participant metadata traversal for channel/newsletter');
        }
    } catch (err) {
        logger.debug({ jid: groupJid }, 'Could not fetch group metadata for profile photo sync');
    }
}
