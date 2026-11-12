import { WAMessage } from '@whiskeysockets/baileys';

// Private media locators are usable only by this paired bridge. Keep the raw
// Baileys message in memory for one bounded handoff; never publish it.
const MAX_ITEMS = 500;
const TTL_MS = 10 * 60 * 1000;
const cache = new Map<string, { message: WAMessage; expiresAt: number }>();

export function rememberMedia(message: WAMessage): void {
    const id = message.key?.id;
    if (!id || !message.message) return;
    cache.set(id, { message, expiresAt: Date.now() + TTL_MS });
    while (cache.size > MAX_ITEMS) cache.delete(cache.keys().next().value as string);
}

export function takeMedia(messageId: string): WAMessage | null {
    const item = cache.get(messageId);
    if (!item || item.expiresAt < Date.now()) {
        cache.delete(messageId);
        return null;
    }
    cache.delete(messageId);
    return item.message;
}
