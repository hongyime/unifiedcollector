import {
    handleBackfillRequest,
    handleGetQr,
    handleJoinGroup,
    handleLogout,
    handleSendMedia,
} from '../../services/wa-client-ts/src/http_routes';
import fs from 'fs';
import path from 'path';
import {
    getBackfillCorrelation,
    resetRuntimeState,
    setQrConnected,
    setQrWaiting,
} from '../../services/wa-client-ts/src/runtime_state';

describe('wa-client-ts HTTP routes', () => {
    const sessionName = 'session_1';

    function createMockRes() {
        const res: any = {};
        res.statusCode = 200;
        res.headers = {};
        res.status = jest.fn((code: number) => {
            res.statusCode = code;
            return res;
        });
        res.set = jest.fn((name: string, value: string) => {
            res.headers[name] = value;
            return res;
        });
        res.json = jest.fn((body: any) => {
            res.body = body;
            return res;
        });
        return res;
    }

    beforeEach(() => {
        resetRuntimeState(sessionName);
        process.env.LINK_DISCOVERY_MAX_JOINS_PER_HOUR = '3';
    });

    it('returns qr snapshot with qr only while waiting', async () => {
        setQrWaiting(sessionName, 'base64-qr');
        const res = createMockRes();

        await handleGetQr({ getSessionName: () => sessionName, getSocket: () => null }, {} as any, res);

        expect(res.status).toHaveBeenCalledWith(200);
        expect(res.body).toEqual({
            status: 'waiting',
            qr: expect.any(String),
            session_name: sessionName,
        });

        setQrConnected(sessionName);
        const nextRes = createMockRes();
        await handleGetQr({ getSessionName: () => sessionName, getSocket: () => null }, {} as any, nextRes);

        expect(nextRes.body).toEqual({
            status: 'connected',
            qr: null,
            session_name: sessionName,
        });
    });

    it('accepts backfill requests and stores correlation mapping', async () => {
        const sock = {
            fetchMessageHistory: jest.fn().mockResolvedValue('request-123'),
        };
        const res = createMockRes();

        await handleBackfillRequest(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            {
                body: {
                    chat_jid: '12345@g.us',
                    oldest_msg_key: { remoteJid: '12345@g.us', id: 'msg-1', fromMe: false },
                    oldest_msg_ts: 1700000000,
                    count: 50,
                    correlation_id: 'corr-123',
                },
            } as any,
            res,
        );

        expect(sock.fetchMessageHistory).toHaveBeenCalledWith(50, { remoteJid: '12345@g.us', id: 'msg-1', fromMe: false }, 1700000000);
        expect(res.status).toHaveBeenCalledWith(200);
        expect(res.body).toEqual({ request_id: 'request-123' });
        expect(getBackfillCorrelation('request-123')).toBe('corr-123');
    });

    it('rejects invalid backfill requests with 400', async () => {
        const sock = {
            fetchMessageHistory: jest.fn(),
        };
        const res = createMockRes();

        await handleBackfillRequest(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            { body: { count: 25 } } as any,
            res,
        );

        expect(res.status).toHaveBeenCalledWith(400);
        expect(res.body).toEqual({ error: 'Missing required field: chat_jid' });
        expect(sock.fetchMessageHistory).not.toHaveBeenCalled();
    });

    it('joins groups and enforces per-session rate limits', async () => {
        const sock = {
            groupAcceptInvite: jest.fn().mockResolvedValue(undefined),
        };

        for (let i = 0; i < 3; i++) {
            const okRes = createMockRes();
            await handleJoinGroup(
                { getSessionName: () => sessionName, getSocket: () => sock as any },
                { body: { invite_code: 'AbCdEfGh1234', session_name: sessionName } } as any,
                okRes,
            );
            expect(okRes.status).toHaveBeenCalledWith(200);
        }

        const limitedRes = createMockRes();
        await handleJoinGroup(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            { body: { invite_code: 'AbCdEfGh1234', session_name: sessionName } } as any,
            limitedRes,
        );

        expect(limitedRes.status).toHaveBeenCalledWith(429);
        expect(limitedRes.body).toMatchObject({ error: 'Rate limit exceeded' });
    });

    it('maps already-member join failures to 409', async () => {
        const sock = {
            groupAcceptInvite: jest.fn().mockRejectedValue(new Error('already a member')),
        };
        const res = createMockRes();

        await handleJoinGroup(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            { body: { invite_code: 'AbCdEfGh1234', session_name: sessionName } } as any,
            res,
        );

        expect(res.status).toHaveBeenCalledWith(409);
        expect(res.body).toEqual({ error: 'Already a member of this group' });
    });

    it('rejects session name mismatches', async () => {
        const sock = {
            groupAcceptInvite: jest.fn(),
        };
        const res = createMockRes();

        await handleJoinGroup(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            { body: { invite_code: 'AbCdEfGh1234', session_name: 'different_session' } } as any,
            res,
        );

        expect(res.status).toHaveBeenCalledWith(400);
        expect(res.body).toEqual({ error: 'session_name does not match active session' });
        expect(sock.groupAcceptInvite).not.toHaveBeenCalled();
    });

    it('logs out the active session on request', async () => {
        const sock = {
            logout: jest.fn().mockResolvedValue(undefined),
        };
        const res = createMockRes();

        await handleLogout(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            { body: { session_name: sessionName } } as any,
            res,
        );

        expect(sock.logout).toHaveBeenCalledTimes(1);
        expect(res.status).toHaveBeenCalledWith(200);
        expect(res.body).toEqual({
            message: 'Logout requested',
            session_name: sessionName,
        });
    });

    it('rejects logout when session_name does not match active socket session', async () => {
        const sock = {
            logout: jest.fn(),
        };
        const res = createMockRes();

        await handleLogout(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            { body: { session_name: 'session_2' } } as any,
            res,
        );

        expect(res.status).toHaveBeenCalledWith(400);
        expect(res.body).toEqual({ error: 'session_name does not match active session' });
        expect(sock.logout).not.toHaveBeenCalled();
    });

    it('sends media from approved storage path', async () => {
        const mediaRoot = path.join(process.cwd(), 'tmp-media-send');
        fs.mkdirSync(mediaRoot, { recursive: true });
        const filePath = path.join(mediaRoot, 'sample.bin');
        fs.writeFileSync(filePath, Buffer.from('hello-media'));

        const previousMediaPath = process.env.MEDIA_STORAGE_PATH;
        process.env.MEDIA_STORAGE_PATH = mediaRoot;

        const sock = {
            sendMessage: jest.fn().mockResolvedValue({ key: { id: 'wamid.123' } }),
        };
        const res = createMockRes();

        await handleSendMedia(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            {
                body: {
                    session_name: sessionName,
                    target_chat_jid: '12345@g.us',
                    file_path: filePath,
                    mimetype: 'application/octet-stream',
                },
            } as any,
            res,
        );

        expect(sock.sendMessage).toHaveBeenCalledTimes(1);
        expect(res.status).toHaveBeenCalledWith(200);
        expect(res.body).toEqual({ message_id: 'wamid.123' });

        process.env.MEDIA_STORAGE_PATH = previousMediaPath;
        fs.rmSync(mediaRoot, { recursive: true, force: true });
    });

    it('rejects send-media requests outside approved media root', async () => {
        const previousMediaPath = process.env.MEDIA_STORAGE_PATH;
        process.env.MEDIA_STORAGE_PATH = '/data/media';

        const sock = {
            sendMessage: jest.fn(),
        };
        const res = createMockRes();

        await handleSendMedia(
            { getSessionName: () => sessionName, getSocket: () => sock as any },
            {
                body: {
                    session_name: sessionName,
                    target_chat_jid: '12345@g.us',
                    file_path: path.join(process.cwd(), 'outside.bin'),
                },
            } as any,
            res,
        );

        expect(res.status).toHaveBeenCalledWith(400);
        expect(res.body).toEqual({ error: 'file_path must be inside MEDIA_STORAGE_PATH' });
        expect(sock.sendMessage).not.toHaveBeenCalled();

        process.env.MEDIA_STORAGE_PATH = previousMediaPath;
    });
});
