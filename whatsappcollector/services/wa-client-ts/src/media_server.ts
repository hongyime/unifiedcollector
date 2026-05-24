import express, { Request, Response, Router } from 'express';
import crypto from 'crypto';
import fs from 'fs';
import pino from 'pino';
import { downloadMedia } from './media_handler';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });
const secret = process.env.MEDIA_BRIDGE_SECRET || '';

if (!secret) {
    logger.warn('MEDIA_BRIDGE_SECRET is not set! HMAC signature verification will reject all requests.');
}

let activeDownloads = 0;
const MAX_CONCURRENT_DOWNLOADS = 10;

// Middleware for HMAC-SHA256 signature verification
const verifySignature = (req: Request, res: Response, next: any) => {
    const signature = req.headers['x-signature'] as string;
    if (!signature) {
        return res.status(401).json({ error: 'Missing X-Signature header' });
    }

    // Use the raw request body bytes for HMAC to ensure byte-identical comparison
    // with the Python side which signs the exact JSON bytes it sends
    const rawBody = (req as any).rawBody;
    if (!rawBody) {
        return res.status(400).json({ error: 'Missing request body' });
    }

    const expectedSignature = crypto.createHmac('sha256', secret).update(rawBody).digest('hex');

    // Use timing-safe comparison to prevent side-channel attacks
    const sigBuf = Buffer.from(signature, 'utf-8');
    const expectedBuf = Buffer.from(expectedSignature, 'utf-8');
    if (sigBuf.length !== expectedBuf.length || !crypto.timingSafeEqual(sigBuf, expectedBuf)) {
        return res.status(401).json({ error: 'Invalid signature' });
    }

    next();
};

const mediaRouter = Router();

mediaRouter.use(express.json({
    limit: '50mb',
    verify: (req: any, _res, buf) => {
        // Store the raw body buffer for HMAC verification
        req.rawBody = buf;
    }
}));

mediaRouter.use((req, res, next) => {
    const start = Date.now();
    res.on('finish', () => {
        const duration = Date.now() - start;
        logger.info({
            method: req.method,
            path: req.originalUrl,
            status: res.statusCode,
            duration: `${duration}ms`
        });
    });
    // Global timeout of 30s as per requirements
    req.setTimeout(30000);
    res.setTimeout(30000);
    next();
});

mediaRouter.post('/media/decrypt', verifySignature, async (req: Request, res: Response) => {
    if (activeDownloads >= MAX_CONCURRENT_DOWNLOADS) {
        return res.status(429).json({ error: 'Too many concurrent downloads' });
    }

    activeDownloads++;
    try {
        const { messageKey, mediaType, rawPayload } = req.body;

        if (!rawPayload) {
            return res.status(400).json({ error: 'Missing rawPayload' });
        }

        const result = await downloadMedia(rawPayload);

        if (!result) {
            return res.status(500).json({ error: 'Failed to download or decrypt media' });
        }

        // As per task MEDIA-002, the TS bridge should return the buffer as application/octet-stream
        // But since downloadMedia streams directly to MEDIA_STORAGE_PATH, we can just return the local file path
        // and let the Python downloader handle it, OR we read the file and send it. 
        // We'll read the file and send it to bridge the gap, then Python saves its own copy or uses the same volume
        // Actually, since both services mount the same media_storage volume (as per INFRA-002),
        // we can just return the path to the Python worker and Python worker doesn't need to re-download if it's there.
        // The PRD says "Return buffer as application/octet-stream", so we will do that to strictly follow the task.

        if (fs.existsSync(result.localPath)) {
            const buffer = fs.readFileSync(result.localPath);
            res.setHeader('Content-Type', 'application/octet-stream');
            res.setHeader('Content-Length', buffer.length.toString());
            res.setHeader('X-Media-SHA256', result.sha256);
            res.setHeader('X-Media-MimeType', result.mimeType);
            res.setHeader('X-Local-Path', result.localPath); // Give python the shortcut just in case
            return res.send(buffer);
        } else {
            return res.status(404).json({ error: 'Media file not found after download' });
        }

    } catch (err: any) {
        logger.error({ err }, '/media/decrypt handler error');
        return res.status(500).json({ error: 'Internal Server Error' });
    } finally {
        activeDownloads--;
    }
});

/**
 * Mount media routes onto an existing Express app.
 * This avoids creating a second HTTP server and the EADDRINUSE crash.
 */
export function mountMediaRoutes(app: express.Application) {
    app.use(mediaRouter);
    logger.info('Media Bridge routes mounted on main HTTP server');
}
