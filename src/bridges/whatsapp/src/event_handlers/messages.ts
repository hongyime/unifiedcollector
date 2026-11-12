// Live message handler. Each incoming WhatsApp message is normalized and
// published with a 'messages.*' routing key so the Python collector's
// 'messages.#' bound queue receives it.

import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { normalizeMessage } from '../utils/normalize';
import { rememberMedia } from '../media_cache';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

function toTs(t: any): number {
    if (typeof t === 'number') return t;
    return t?.low || (typeof t?.toNumber === 'function' ? t.toNumber() : Math.floor(Date.now() / 1000));
}

function actorJid(sock: WASocket, msg: any): string {
    if (msg.key?.fromMe) return (sock as any).user?.id || '';
    return msg.key?.participant || msg.key?.remoteJid || '';
}

export function registerMessagesHandler(sock: WASocket): void {
    sock.ev.on('messages.upsert', async (event) => {
        try {
            const accountWid = (sock as any).user?.id || null;
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
                        account_wid: accountWid,
                        routing_key: 'messages.delete',
                    });
                    continue;
                }
                const reaction: any = (msg.message as any)?.reactionMessage;
                if (reaction?.key?.id) {
                    const chatJid = reaction.key.remoteJid || msg.key?.remoteJid || '';
                    const actor = actorJid(sock, msg);
                    if (!chatJid || !actor) continue;
                    await producer.publish('messages.reaction', {
                        reaction: true,
                        message_id: msg.key?.id || '',
                        reaction_message_id: msg.key?.id || '',
                        target_message_id: reaction.key.id,
                        chat_jid: chatJid,
                        actor_jid: actor,
                        emoji: typeof reaction.text === 'string' && reaction.text.trim() ? reaction.text : null,
                        timestamp: toTs(reaction.senderTimestampMs || msg.messageTimestamp),
                        session_name: process.env.SESSION_NAME || 'default',
                        account_wid: accountWid,
                        routing_key: 'messages.reaction',
                    });
                    continue;
                }
                const rawMessage: any = msg.message;
                const poll = rawMessage?.pollCreationMessage || rawMessage?.pollCreationMessageV2 ||
                    rawMessage?.pollCreationMessageV3 || rawMessage?.pollCreationMessageV5;
                if (poll) {
                    const chatJid = msg.key?.remoteJid || '';
                    const creator = actorJid(sock, msg);
                    if (!msg.key?.id || !chatJid || !creator) continue;
                    await producer.publish('messages.poll', {
                        poll: true,
                        message_id: msg.key.id,
                        chat_jid: chatJid,
                        sender_jid: creator,
                        chat_name: (msg as any).pushName || chatJid.split('@')[0],
                        question: typeof poll.name === 'string' ? poll.name : '',
                        options: (poll.options || []).map((option: any) => option?.optionName).filter((option: any) => typeof option === 'string'),
                        selectable_options_count: Number(poll.selectableOptionsCount || 0) || null,
                        poll_type: poll.pollType === 1 ? 'quiz' : 'poll',
                        timestamp: toTs(msg.messageTimestamp),
                        session_name: process.env.SESSION_NAME || 'default',
                        account_wid: accountWid,
                        routing_key: 'messages.poll',
                    });
                    continue;
                }
                const norm = normalizeMessage(msg);
                if (!norm) continue;
                if (norm.message_type === 'unknown') continue;
                if (norm.has_media) rememberMedia(msg);
                await producer.publish(norm.routing_key, {
                    ...norm,
                    session_name: process.env.SESSION_NAME || 'default',
                    account_wid: accountWid,
                });
            }
        } catch (err) {
            logger.error({ err }, 'Error in messages.upsert handler');
        }
    });
}
