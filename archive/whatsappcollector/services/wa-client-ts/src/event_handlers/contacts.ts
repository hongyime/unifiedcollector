import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import { profilePhotoFetcher } from '../profile_photo_fetcher';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

function normalizeJid(jid: string) {
    if (!jid) return '';
    return jid.replace(/:[0-9]+/, '');
}

export function registerContactsHandler(sock: WASocket) {
    sock.ev.on('contacts.update', async (contacts) => {
        try {
            for (const contact of contacts) {
                await processContact(contact);
            }
        } catch (err) {
            logger.error({ err }, 'Error in contacts.update handler');
        }
    });

    sock.ev.on('contacts.upsert', async (contacts) => {
        try {
            for (const contact of contacts) {
                await processContact(contact);
            }
        } catch (err) {
            logger.error({ err }, 'Error in contacts.upsert handler');
        }
    });
}

async function processContact(contact: any) {
    const rawJid = contact.id;
    if (!rawJid) return;

    const jid = normalizeJid(rawJid);
    const isLid = jid.includes('@lid');

    const displayName = contact.notify || contact.name || contact.verifiedName || null;
    let phoneNumber = null;
    if (!isLid && jid.includes('@s.whatsapp.net')) {
        phoneNumber = jid.split('@')[0];
    }

    const payload = {
        jid: isLid ? null : jid,
        lid: isLid ? jid : null,
        display_name: displayName,
        phone_number: phoneNumber
    };

    await producer.publish('contacts.update', payload);

    if (jid && !isLid) {
        profilePhotoFetcher.enqueue(jid);
    }
}
