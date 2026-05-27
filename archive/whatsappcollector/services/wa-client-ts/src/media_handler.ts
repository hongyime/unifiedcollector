import { downloadContentFromMessage, MediaType, WAMessage } from '@whiskeysockets/baileys';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import pino from 'pino';
import { pipeline } from 'stream/promises';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

const MEDIA_STORAGE_PATH = process.env.MEDIA_STORAGE_PATH || '/data/media';

const MIME_MAP: Record<string, string> = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'video/mp4': '.mp4',
    'audio/ogg; codecs=opus': '.ogg',
    'audio/mpeg': '.mp3',
    'audio/aac': '.aac',
    'application/pdf': '.pdf',
};

// WhatsApp CDN request headers — required for newsletter/channel media which is served
// without per-message encryption (no mediaKey). Regular encrypted media uses these too.
const WA_CDN_HEADERS: Record<string, string> = {
    Origin: 'https://web.whatsapp.com',
    Referer: 'https://web.whatsapp.com/',
    'User-Agent': 'WhatsApp/2.3000.1035194821 A',
};

function getMediaContent(msg: WAMessage): { mediaType: MediaType, content: any } | null {
    if (!msg.message) return null;

    if (msg.message.imageMessage) return { mediaType: 'image', content: msg.message.imageMessage };
    if (msg.message.videoMessage) return { mediaType: 'video', content: msg.message.videoMessage };
    if (msg.message.audioMessage) return { mediaType: 'audio', content: msg.message.audioMessage };
    if (msg.message.documentMessage) return { mediaType: 'document', content: msg.message.documentMessage };
    if (msg.message.stickerMessage) return { mediaType: 'image', content: msg.message.stickerMessage };

    return null;
}

function safeDir(jid: string): string {
    return jid.replace(/[^a-zA-Z0-9@._-]/g, '_');
}

// Download newsletter/channel media directly from the CDN.
// WhatsApp Channels do NOT use per-message AES encryption — the content is uploaded
// as raw bytes and served directly from mmg.whatsapp.net with a time-limited signed URL.
// No mediaKey is needed; the signed URL itself is the access control.
async function downloadNewsletterMedia(
    content: { url?: string; directPath?: string; mimetype?: string },
    chatJid: string
): Promise<{ localPath: string; sha256: string; mimeType: string; fileSize: number } | null> {
    const downloadUrl: string | null =
        content.url ||
        (content.directPath ? `https://mmg.whatsapp.net${content.directPath}` : null);
    if (!downloadUrl) {
        logger.warn({ chatJid }, 'Newsletter media has no URL or directPath');
        return null;
    }

    const mimeType = content.mimetype || 'application/octet-stream';
    const ext = MIME_MAP[mimeType] || '';

    try {
        const resp = await fetch(downloadUrl, {
            headers: WA_CDN_HEADERS,
            signal: AbortSignal.timeout(30_000),
        });

        if (!resp.ok) {
            logger.warn({ status: resp.status, url: downloadUrl.slice(0, 80) }, 'Newsletter media CDN returned error');
            return null;
        }

        const arrayBuf = await resp.arrayBuffer();
        const buffer = Buffer.from(arrayBuf);
        const sha256 = crypto.createHash('sha256').update(buffer).digest('hex');

        const chatDir = path.join(MEDIA_STORAGE_PATH, safeDir(chatJid));
        if (!fs.existsSync(chatDir)) fs.mkdirSync(chatDir, { recursive: true });

        const localPath = path.join(chatDir, `${sha256}${ext}`);
        if (!fs.existsSync(localPath)) fs.writeFileSync(localPath, buffer);

        logger.info({ bytes: buffer.length, chatJid }, 'Newsletter media downloaded directly from CDN');
        return { localPath, sha256, mimeType, fileSize: buffer.length };
    } catch (err: any) {
        logger.warn({ err, chatJid }, 'Newsletter media direct download error');
        return null;
    }
}

export async function downloadMedia(msg: WAMessage, maxRetries = 3) {
    const mediaInfo = getMediaContent(msg);
    if (!mediaInfo) return null;

    const { mediaType, content } = mediaInfo;
    const chatJid = msg.key.remoteJid;
    if (!chatJid) return null;

    const mimeType = content.mimetype || 'application/octet-stream';
    const ext = MIME_MAP[mimeType] || '';

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            const stream = await downloadContentFromMessage(content, mediaType);

            const chunks: Buffer[] = [];
            for await (const chunk of stream) {
                chunks.push(chunk);
            }
            const buffer = Buffer.concat(chunks);

            const sha256 = crypto.createHash('sha256').update(buffer).digest('hex');

            const chatDir = path.join(MEDIA_STORAGE_PATH, chatJid);
            if (!fs.existsSync(chatDir)) {
                fs.mkdirSync(chatDir, { recursive: true });
            }

            const localPath = path.join(chatDir, `${sha256}${ext}`);

            if (!fs.existsSync(localPath)) {
                fs.writeFileSync(localPath, buffer);
            }

            return {
                localPath,
                sha256,
                mimeType,
                fileSize: buffer.length
            };
        } catch (err: any) {
            // Newsletter/channel messages have no mediaKey (NOT per-message encrypted).
            // Fall back to a direct CDN fetch — no decryption step needed.
            if (err.message?.includes('empty media key')) {
                logger.debug({ msgId: msg.key.id }, 'No mediaKey — attempting direct CDN download (newsletter/channel)');
                return downloadNewsletterMedia(content, chatJid);
            }

            logger.warn({ err, msgId: msg.key.id, attempt }, 'Error downloading media');
            if (err.output?.statusCode === 410 || attempt === maxRetries) {
                if (attempt === maxRetries) {
                    logger.error({ msgId: msg.key.id }, 'Max retries reached for downloading media');
                }
                return null;
            }
            // Exponential backoff
            await new Promise(res => setTimeout(res, 1000 * Math.pow(2, attempt)));
        }
    }
    return null;
}
