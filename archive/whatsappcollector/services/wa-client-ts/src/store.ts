import { WAMessage } from '@whiskeysockets/baileys';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

// makeInMemoryStore is not exported in all Baileys versions; load it safely.
let makeInMemoryStore: any;
try {
    makeInMemoryStore = require('@whiskeysockets/baileys').makeInMemoryStore;
} catch (_) { /* not available */ }

// Create an in-memory store for messages (useful for message retry decryption)
export const store = makeInMemoryStore ? makeInMemoryStore({ logger }) : null;

export function bindStore(sock: any) {
    if (store) store.bind(sock.ev);
}

export async function getMessage(key: any): Promise<WAMessage["message"] | undefined> {
    if (store) {
        const msg = await store.loadMessage(key.remoteJid!, key.id!);
        return msg?.message || undefined;
    }
    return undefined;
}
