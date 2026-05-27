import express, { Request, Response } from 'express';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { WASocket } from '@whiskeysockets/baileys';
import pino from 'pino';
import * as QRCode from 'qrcode';
import {
    getBackfillCorrelation,
    getQrSnapshot,
    recordJoinAttempt,
    setQrConnected,
    setQrError,
    setQrScanned,
    setQrWaiting,
    storeBackfillCorrelation,
} from './runtime_state';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export interface WaClientRoutesDeps {
    getSocket: () => WASocket | null;
    getSessionName: () => string;
}

function getInviteCode(rawInvite: unknown): string | null {
    if (typeof rawInvite !== 'string') return null;
    const invite = rawInvite.trim();
    if (!invite) return null;

    const match = invite.match(/(?:chat\.whatsapp\.com\/)?([A-Za-z0-9_-]{6,})/);
    return match ? match[1] : null;
}

function getRequestId(result: any): string | null {
    if (typeof result === 'string' && result.trim()) return result.trim();
    if (result && typeof result === 'object') {
        return result.request_id || result.requestId || result.requestID || result.id || null;
    }
    return null;
}

function normalizeSessionName(value: unknown): string | null {
    if (typeof value !== 'string') return null;
    const session = value.trim();
    return session ? session : null;
}

function verifySignedBridgeRequest(req: Request, res: Response, next: () => void) {
    const secret = (process.env.MEDIA_BRIDGE_SECRET || '').trim();
    if (!secret) {
        return res.status(503).json({ error: 'MEDIA_BRIDGE_SECRET is not configured' });
    }

    const signature = req.headers['x-signature'] as string;
    if (!signature) {
        return res.status(401).json({ error: 'Missing X-Signature header' });
    }

    const rawBody = (req as any).rawBody as Buffer | undefined;
    if (!rawBody) {
        return res.status(400).json({ error: 'Missing request body' });
    }

    const expected = crypto.createHmac('sha256', secret).update(rawBody).digest('hex');
    const sigBuf = Buffer.from(signature, 'utf-8');
    const expectedBuf = Buffer.from(expected, 'utf-8');
    if (sigBuf.length !== expectedBuf.length || !crypto.timingSafeEqual(sigBuf, expectedBuf)) {
        return res.status(401).json({ error: 'Invalid signature' });
    }

    next();
}

function isAllowedMediaPath(filePath: string): boolean {
    const allowedRoots = [process.env.MEDIA_STORAGE_PATH || '/data/media']
        .map((root) => path.resolve(root));
    const resolved = path.resolve(filePath);
    return allowedRoots.some((root) => resolved === root || resolved.startsWith(`${root}${path.sep}`));
}

export async function handleGetQr(deps: WaClientRoutesDeps, _req: Request, res: Response) {
    const snapshot = getQrSnapshot(deps.getSessionName());
    let qrPngBase64: string | null = null;

    if (snapshot.status === 'waiting' && snapshot.qr) {
        try {
            // Encode the raw Baileys QR data string to a base64 PNG so the
            // collector dashboard can render it with st.image(base64.b64decode(...))
            const pngDataUrl = await QRCode.toDataURL(snapshot.qr, { type: 'image/png', scale: 6 });
            // Strip the "data:image/png;base64," prefix — return raw base64 only
            qrPngBase64 = pngDataUrl.split(',')[1] ?? null;
        } catch (err) {
            logger.error({ err }, 'Failed to encode QR to PNG');
        }
    }

    res.status(200).json({
        status: snapshot.status,
        qr: qrPngBase64,
        session_name: snapshot.session_name,
    });
}

export async function handleBackfillRequest(deps: WaClientRoutesDeps, req: Request, res: Response) {
    const sock = deps.getSocket();
    if (!sock) {
        return res.status(503).json({ error: 'WhatsApp socket is not ready' });
    }

    const { chat_jid, oldest_msg_key, oldest_msg_ts, count, correlation_id } = req.body || {};

    if (typeof chat_jid !== 'string' || !chat_jid.trim()) {
        return res.status(400).json({ error: 'Missing required field: chat_jid' });
    }
    if (!oldest_msg_key || typeof oldest_msg_key !== 'object') {
        return res.status(400).json({ error: 'Missing required field: oldest_msg_key' });
    }
    if (typeof oldest_msg_ts !== 'number' || Number.isNaN(oldest_msg_ts)) {
        return res.status(400).json({ error: 'Missing required field: oldest_msg_ts' });
    }
    if (typeof count !== 'number' || !Number.isInteger(count) || count <= 0) {
        return res.status(400).json({ error: 'Missing required field: count' });
    }
    if (typeof correlation_id !== 'string' || !correlation_id.trim()) {
        return res.status(400).json({ error: 'Missing required field: correlation_id' });
    }

    try {
        const requestResult = await (sock as any).fetchMessageHistory(count, oldest_msg_key, oldest_msg_ts);
        const requestId = getRequestId(requestResult) || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        storeBackfillCorrelation(requestId, correlation_id.trim());
        return res.status(200).json({ request_id: requestId });
    } catch (err) {
        logger.error({ err }, 'Failed to request message history backfill');
        return res.status(502).json({ error: 'Failed to request message history' });
    }
}

export async function handleJoinGroup(deps: WaClientRoutesDeps, req: Request, res: Response) {
    const sock = deps.getSocket();
    if (!sock) {
        return res.status(503).json({ error: 'WhatsApp socket is not ready' });
    }

    const inviteCode = getInviteCode(req.body?.invite_code);
    if (!inviteCode) {
        return res.status(400).json({ error: 'Invalid invite code format' });
    }

    const requestSessionName = normalizeSessionName(req.body?.session_name);
    const sessionName = deps.getSessionName();
    if (!requestSessionName) {
        return res.status(400).json({ error: 'Missing required field: session_name' });
    }
    if (requestSessionName !== sessionName) {
        return res.status(400).json({ error: 'session_name does not match active session' });
    }

    const limitState = recordJoinAttempt(sessionName);
    if (!limitState.allowed) {
        res.set('Retry-After', String(limitState.retryAfterSeconds));
        return res.status(429).json({
            error: 'Rate limit exceeded',
            retry_after: limitState.retryAfterSeconds,
        });
    }

    try {
        await (sock as any).groupAcceptInvite(inviteCode);
        return res.status(200).json({ message: 'Joined group successfully' });
    } catch (err: any) {
        const message = String(err?.message || err || '').toLowerCase();
        if (message.includes('already') || message.includes('member') || message.includes('duplicate')) {
            return res.status(409).json({ error: 'Already a member of this group' });
        }
        if (message.includes('rate') || message.includes('429') || message.includes('too many')) {
            res.set('Retry-After', '3600');
            return res.status(429).json({
                error: 'Rate limit exceeded',
                retry_after: 3600,
            });
        }

        logger.error({ err }, 'Failed to join WhatsApp group');
        return res.status(502).json({ error: 'Failed to join group' });
    }
}

export async function handleLogout(deps: WaClientRoutesDeps, req: Request, res: Response) {
    const sock = deps.getSocket();
    if (!sock) {
        return res.status(503).json({ error: 'WhatsApp socket is not ready' });
    }

    const requestSessionName = normalizeSessionName(req.body?.session_name);
    const sessionName = deps.getSessionName();
    if (!requestSessionName) {
        return res.status(400).json({ error: 'Missing required field: session_name' });
    }
    if (requestSessionName !== sessionName) {
        return res.status(400).json({ error: 'session_name does not match active session' });
    }

    try {
        const logoutFn = (sock as any).logout;
        if (typeof logoutFn === 'function') {
            await logoutFn.call(sock);
        } else {
            (sock as any).ws?.close?.();
        }
        return res.status(200).json({
            message: 'Logout requested',
            session_name: sessionName,
        });
    } catch (err) {
        logger.error({ err }, 'Failed to logout session');
        return res.status(502).json({ error: 'Failed to logout session' });
    }
}

export async function handleSendMedia(deps: WaClientRoutesDeps, req: Request, res: Response) {
    const sock = deps.getSocket();
    if (!sock) {
        return res.status(503).json({ error: 'WhatsApp socket is not ready' });
    }

    const requestSessionName = normalizeSessionName(req.body?.session_name);
    const sessionName = deps.getSessionName();
    if (!requestSessionName) {
        return res.status(400).json({ error: 'Missing required field: session_name' });
    }
    if (requestSessionName !== sessionName) {
        return res.status(400).json({ error: 'session_name does not match active session' });
    }

    const targetChatJid = typeof req.body?.target_chat_jid === 'string' ? req.body.target_chat_jid.trim() : '';
    const filePath = typeof req.body?.file_path === 'string' ? req.body.file_path.trim() : '';
    const caption = typeof req.body?.caption === 'string' ? req.body.caption : undefined;
    const mimeType = typeof req.body?.mimetype === 'string' ? req.body.mimetype : 'application/octet-stream';

    if (!targetChatJid) {
        return res.status(400).json({ error: 'Missing required field: target_chat_jid' });
    }
    if (!filePath) {
        return res.status(400).json({ error: 'Missing required field: file_path' });
    }
    if (!isAllowedMediaPath(filePath)) {
        return res.status(400).json({ error: 'file_path must be inside MEDIA_STORAGE_PATH' });
    }
    if (!fs.existsSync(filePath)) {
        return res.status(404).json({ error: 'file_path does not exist' });
    }

    try {
        const fileBuffer = fs.readFileSync(filePath);
        const fileName = path.basename(filePath);

        let message: any = {
            document: fileBuffer,
            fileName,
            mimetype: mimeType,
            caption,
        };

        if (mimeType.startsWith('image/')) {
            message = { image: fileBuffer, caption };
        } else if (mimeType.startsWith('video/')) {
            message = { video: fileBuffer, caption };
        } else if (mimeType.startsWith('audio/')) {
            message = { audio: fileBuffer, mimetype: mimeType };
        }

        const result = await (sock as any).sendMessage(targetChatJid, message);
        const messageId = result?.key?.id || null;
        return res.status(200).json({ message_id: messageId });
    } catch (err) {
        logger.error({ err }, 'Failed to send outbound media');
        return res.status(502).json({ error: 'Failed to send media' });
    }
}

export function registerWaClientRoutes(app: express.Application, deps: WaClientRoutesDeps) {
    const router = express.Router();

    router.use(express.json({
        limit: '4mb',
        verify: (req: any, _res, buf) => {
            req.rawBody = buf;
        }
    }));

    router.get('/qr', (req: Request, res: Response) => void handleGetQr(deps, req, res));

    router.get('/list-groups', async (req: Request, res: Response) => {
        const sock = deps.getSocket();
        if (!sock) return res.status(503).json({ error: 'Socket not ready' });
        try {
            const groups = await (sock as any).groupFetchAllParticipating();
            const list = Object.values(groups as Record<string, any>).map((g: any) => ({
                jid: g.id,
                name: g.subject,
                member_count: g.participants?.length ?? 0,
            }));
            list.sort((a: any, b: any) => a.name.localeCompare(b.name));
            return res.status(200).json({ groups: list, count: list.length });
        } catch (err) {
            logger.error({ err }, 'list-groups failed');
            return res.status(502).json({ error: 'Failed to fetch groups' });
        }
    });

    router.post('/backfill-request', (req: Request, res: Response) => void handleBackfillRequest(deps, req, res));

    router.post('/join-group', (req: Request, res: Response) => void handleJoinGroup(deps, req, res));

    router.post('/logout', (req: Request, res: Response) => void handleLogout(deps, req, res));

    router.post('/send-media', verifySignedBridgeRequest, (req: Request, res: Response) => void handleSendMedia(deps, req, res));

    app.use(router);
}

export function updateQrStateFromConnection(update: any, sessionName: string) {
    if (update?.qr) {
        setQrWaiting(sessionName, update.qr);
        return;
    }

    if (update?.connection === 'open') {
        setQrConnected(sessionName);
        return;
    }

    if (update?.connection === 'close') {
        const reason = update?.lastDisconnect?.error?.message || update?.lastDisconnect?.error?.output?.statusCode || 'connection_closed';
        setQrError(sessionName, String(reason));
        return;
    }

    if (update?.connection === 'connecting') {
        const current = getQrSnapshot(sessionName);
        if (current.qr) {
            setQrScanned(sessionName);
        }
    }
}

export function getCorrelationForRequest(requestId: string) {
    return getBackfillCorrelation(requestId);
}
