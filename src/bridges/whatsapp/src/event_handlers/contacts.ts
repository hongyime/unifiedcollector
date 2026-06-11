// Contacts handler. Publishes contact metadata to 'contacts.update'. The
// primary user-profile population happens via message events (the consumer's
// _track_user_profile reads pushName/sender_jid off each message), so this is
// supplementary -- it gives the collector display names ahead of first message.

import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

function normalizeJid(jid: string): string {
    return jid ? jid.replace(/:[0-9]+/, '') : '';
}

async function processContact(contact: any): Promise<void> {
    const raw = contact.id;
    if (!raw) return;
    const jid = normalizeJid(raw);
    const isLid = jid.includes('@lid');
    const displayName = contact.notify || contact.name || contact.verifiedName || null;
    const phone = !isLid && jid.includes('@s.whatsapp.net') ? jid.split('@')[0] : null;
    // contact.lid is set by Baileys on @s.whatsapp.net contacts to indicate
    // the paired linked-device ID. Publishing it lets the collector maintain
    // a lid → phone_jid mapping table for resolving group message senders.
    const contactLid = contact.lid ? normalizeJid(contact.lid) : null;

    await producer.publish('contacts.update', {
        jid: isLid ? null : jid,
        lid: isLid ? jid : contactLid,
        display_name: displayName,
        phone_number: phone,
    });
}

export function registerContactsHandler(sock: WASocket): void {
    const handler = async (contacts: any[]) => {
        try {
            for (const c of contacts) await processContact(c);
        } catch (err) {
            logger.error({ err }, 'Error in contacts handler');
        }
    };
    sock.ev.on('contacts.update', handler);
    sock.ev.on('contacts.upsert', handler);
}
