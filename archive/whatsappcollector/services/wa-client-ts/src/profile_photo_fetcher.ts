import { WASocket } from '@whiskeysockets/baileys';
import { producer } from './producer';
import pino from 'pino';
import crypto from 'crypto';
import { Buffer } from 'buffer';
import fs from 'fs';
import path from 'path';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });
const MAX_PHOTO_BYTES = 10 * 1024 * 1024; // 10 MB guard — prevents OOM on malicious URLs

interface ProfilePhotoTask {
    jid: string;
}

class ProfilePhotoFetcher {
    private queue: ProfilePhotoTask[] = [];
    private isProcessing: boolean = false;
    private knownHashes: Map<string, string> = new Map(); // jid -> sha256 to avoid redundant broker publishes
    private sock: WASocket | null = null;
    private hashesLoaded: boolean = false;
    private hashStorePath: string;

    constructor() {
        const authStorage = process.env.AUTH_STORAGE_PATH || './auth_info';
        this.hashStorePath = path.join(authStorage, 'profile_photo_hashes.json');
    }

    public setSock(sock: WASocket) {
        this.sock = sock;
        this.loadKnownHashes();
    }

    public saveKnownHashes() {
        try {
            const dir = path.dirname(this.hashStorePath);
            fs.mkdirSync(dir, { recursive: true });
            const serialized = JSON.stringify(Object.fromEntries(this.knownHashes), null, 2);
            fs.writeFileSync(this.hashStorePath, serialized, 'utf-8');
            logger.info({ count: this.knownHashes.size, path: this.hashStorePath }, 'Persisted profile photo hash cache');
        } catch (err: any) {
            logger.warn({ err: String(err) }, 'Failed to persist profile photo hash cache');
        }
    }

    private loadKnownHashes() {
        if (this.hashesLoaded) return;
        this.hashesLoaded = true;

        try {
            if (!fs.existsSync(this.hashStorePath)) {
                return;
            }

            const raw = fs.readFileSync(this.hashStorePath, 'utf-8');
            const parsed = JSON.parse(raw) as Record<string, string>;
            for (const [jid, hash] of Object.entries(parsed || {})) {
                if (jid && hash) {
                    this.knownHashes.set(jid, hash);
                }
            }

            logger.info({ count: this.knownHashes.size, path: this.hashStorePath }, 'Loaded profile photo hash cache');
        } catch (err: any) {
            logger.warn({ err: String(err) }, 'Failed to load profile photo hash cache');
        }
    }

    public enqueue(jid: string) {
        if (!this.queue.some(t => t.jid === jid)) {
            this.queue.push({ jid });
            this.processQueue();
        }
    }

    private async processQueue() {
        if (this.isProcessing) return;
        this.isProcessing = true;

        while (this.queue.length > 0) {
            const task = this.queue.shift();
            if (!task) continue;

            const sock = this.sock;
            if (!sock) {
                // If sock isn't available, put it back and wait
                this.queue.unshift(task);
                await this.sleep(5000);
                continue;
            }

            try {
                // Fetch full resolution profile picture URL
                const url = await sock.profilePictureUrl(task.jid, 'image').catch(() => null);

                if (url) {
                    // Download the image buffer
                    const response = await fetch(url);
                    if (response.ok) {
                        // Guard against oversized responses before buffering
                        const contentLength = Number(response.headers.get('content-length') ?? 0);
                        if (contentLength > MAX_PHOTO_BYTES) {
                            logger.warn({ jid: task.jid, contentLength }, 'Profile photo too large, skipping');
                        } else {
                            const arrayBuffer = await response.arrayBuffer();
                            if (arrayBuffer.byteLength > MAX_PHOTO_BYTES) {
                                logger.warn({ jid: task.jid, bytes: arrayBuffer.byteLength }, 'Profile photo exceeded size limit after download, skipping');
                            } else {
                                const buffer = Buffer.from(arrayBuffer);
                                const hash = crypto.createHash('sha256').update(buffer).digest('hex');

                                const lastHash = this.knownHashes.get(task.jid);
                                if (lastHash !== hash) {
                                    this.knownHashes.set(task.jid, hash);

                                    // Save to shared media volume — avoids sending multi-MB
                                    // base64 payloads through the broker.
                                    const mediaRoot = process.env.MEDIA_STORAGE_PATH || '/data/media';
                                    const photoDir = path.join(mediaRoot, 'profile_photos');
                                    const fileName = `${task.jid.replace(/[^a-zA-Z0-9_.-]/g, '_')}_${hash}.jpg`;
                                    const localPath = path.join(photoDir, fileName);
                                    try {
                                        fs.mkdirSync(photoDir, { recursive: true });
                                        fs.writeFileSync(localPath, buffer);
                                    } catch (writeErr) {
                                        logger.error({ writeErr, jid: task.jid }, 'Failed to save profile photo to disk');
                                    }

                                    // Publish only metadata — consumers read from shared volume
                                    const payload = {
                                        jid: task.jid,
                                        sha256: hash,
                                        local_path: localPath,
                                        file_size: buffer.byteLength,
                                    };
                                    logger.info({ jid: task.jid, sha256: hash, local_path: localPath }, 'Publishing profile photo metadata to broker');
                                    await producer.publish('profile_photo.process', payload);
                                } else {
                                    logger.debug({ jid: task.jid }, 'Profile photo unchanged, skipping publish');
                                }
                            }
                        }
                    } else {
                        logger.warn({ jid: task.jid, status: response.status }, 'Failed to fetch profile photo URL');
                    }
                }
            } catch (err: any) {
                // 401/404 are expected for users with privacy settings hiding their photo
                if (err?.data === 401 || err?.data === 404 || err?.status === 401 || err?.status === 404) {
                    logger.debug({ jid: task.jid }, 'Profile photo not available (privacy or not set)');
                } else {
                    logger.error({ err, jid: task.jid }, 'Error fetching profile photo');
                }
            }

            // Rate limit: ~1 request per 3-5 seconds (3000ms base + 0-2000ms jitter)
            const jitter = Math.floor(Math.random() * 2000);
            await this.sleep(3000 + jitter);
        }

        this.isProcessing = false;
    }

    private sleep(ms: number) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
}

export const profilePhotoFetcher = new ProfilePhotoFetcher();
