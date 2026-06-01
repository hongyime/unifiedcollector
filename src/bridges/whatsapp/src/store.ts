// Minimal in-memory message store. Baileys uses getMessage() for retry/decrypt
// of poll updates and message re-sends. We keep a small LRU-ish cache keyed by
// message id. This is intentionally lightweight (the full upstream store has
// been removed -- the bridge's job is to forward, not to be a source of truth).

import { WASocket, WAMessageKey, proto } from '@whiskeysockets/baileys';

const MAX = 2000;
const cache = new Map<string, proto.IMessage>();

export function bindStore(sock: WASocket): void {
    sock.ev.on('messages.upsert', ({ messages }) => {
        for (const m of messages) {
            if (m.key?.id && m.message) {
                cache.set(m.key.id, m.message);
                if (cache.size > MAX) {
                    const first = cache.keys().next().value;
                    if (first) cache.delete(first);
                }
            }
        }
    });
}

export async function getMessage(key: WAMessageKey): Promise<proto.IMessage | undefined> {
    if (key?.id && cache.has(key.id)) return cache.get(key.id);
    return undefined;
}
