jest.mock('amqplib');
jest.mock('ioredis');
jest.mock('fs');

jest.mock('@whiskeysockets/baileys', () => ({
    useMultiFileAuthState: jest.fn(),
}));

jest.mock('../../services/wa-client-ts/src/producer', () => {
    const mockPublish = jest.fn().mockResolvedValue(undefined);
    return {
        producer: {
            publish: mockPublish,
            connect: jest.fn().mockResolvedValue(undefined),
        },
    };
});

jest.mock('../../services/wa-client-ts/src/utils/normalize', () => ({
    normalizeMessage: jest.fn((msg) => {
        if (!msg.message || !msg.key) return null;
        return {
            message_id: msg.key.id,
            chat_jid: msg.key.remoteJid,
            sender_jid: msg.key.participant || msg.key.remoteJid,
            timestamp: msg.messageTimestamp || 1700000000,
            message_type: 'text',
            body: 'history',
            routing_key: 'history.sync',
        };
    }),
}));

describe('History Sync behavior', () => {
    let fsMock: any;
    let producerMock: any;
    let handlers: Record<string, Function>;
    let mockSock: any;
    let runtimeState: any;

    function createMockSocket() {
        handlers = {};
        return {
            ev: {
                on: jest.fn((event: string, handler: Function) => {
                    handlers[event] = handler;
                }),
            },
        };
    }

    function setupHandler(initialWatermarks = '{}') {
        jest.resetModules();

        fsMock = require('fs');
        producerMock = require('../../services/wa-client-ts/src/producer').producer;
        runtimeState = require('../../services/wa-client-ts/src/runtime_state');

        (fsMock.existsSync as jest.Mock).mockReturnValue(true);
        (fsMock.readFileSync as jest.Mock).mockReturnValue(initialWatermarks);
        (fsMock.writeFileSync as jest.Mock).mockReturnValue(undefined);
        (fsMock.mkdirSync as jest.Mock).mockReturnValue(undefined);

        runtimeState.resetRuntimeState('session_1');

        const { registerHistoryHandler } = require('../../services/wa-client-ts/src/event_handlers/history');
        registerHistoryHandler(mockSock);
    }

    beforeEach(() => {
        jest.useFakeTimers();
        jest.clearAllMocks();
        mockSock = createMockSocket();
        process.env.SESSION_NAME = 'session_1';
    });

    afterEach(() => {
        delete process.env.SESSION_NAME;
        jest.clearAllTimers();
        jest.useRealTimers();
    });

    it('loads persisted watermarks and preserves oldest timestamp', async () => {
        setupHandler(JSON.stringify({
            'chat1@g.us': {
                oldestTimestamp: 100,
                messageCount: 2,
                isComplete: false,
                lastSyncTime: 1,
            },
        }));

        await handlers['messaging-history.set']({
            syncType: 'INITIAL_BOOTSTRAP',
            messages: [
                {
                    key: { id: 'm3', remoteJid: 'chat1@g.us', participant: 'user@s.whatsapp.net' },
                    message: { conversation: 'next' },
                    messageTimestamp: 200,
                },
            ],
            isLatest: false,
        });

        expect(producerMock.publish).toHaveBeenCalledWith('messages.history', expect.objectContaining({
            sync_type: 'INITIAL_BOOTSTRAP',
            correlation_id: null,
            session_name: 'session_1',
            messages: [expect.objectContaining({ message_id: 'm3' })],
        }));
        expect(fsMock.writeFileSync).toHaveBeenCalled();

        const lastWrite = (fsMock.writeFileSync as jest.Mock).mock.calls[(fsMock.writeFileSync as jest.Mock).mock.calls.length - 1][1] as string;
        expect(lastWrite).toContain('"oldestTimestamp": 100');
        expect(lastWrite).toContain('"messageCount": 3');
    });

    it('marks chat sync complete when isLatest is true', async () => {
        setupHandler();

        await handlers['messaging-history.set']({
            syncType: 'ON_DEMAND',
            requestId: 'req-123',
            messages: [
                {
                    key: { id: 'm1', remoteJid: 'chat2@g.us', participant: 'a@s.whatsapp.net' },
                    message: { conversation: 'a' },
                    messageTimestamp: 1700000005,
                },
                {
                    key: { id: 'm2', remoteJid: 'chat2@g.us', participant: 'b@s.whatsapp.net' },
                    message: { conversation: 'b' },
                    messageTimestamp: 1700000000,
                },
            ],
            isLatest: true,
        });

        expect(producerMock.publish).toHaveBeenCalledTimes(1);
        expect(producerMock.publish).toHaveBeenCalledWith('messages.history', expect.objectContaining({
            sync_type: 'ON_DEMAND',
            correlation_id: null,
            session_name: 'session_1',
            messages: expect.arrayContaining([
                expect.objectContaining({ message_id: 'm1' }),
                expect.objectContaining({ message_id: 'm2' }),
            ]),
        }));

        const lastWrite = (fsMock.writeFileSync as jest.Mock).mock.calls[(fsMock.writeFileSync as jest.Mock).mock.calls.length - 1][1] as string;
        expect(lastWrite).toContain('"isComplete": true');
        expect(lastWrite).toContain('"messageCount": 2');
        expect(lastWrite).toContain('"oldestTimestamp": 1700000000');
    });

    it('uses stored correlation id for on-demand history sync', async () => {
        setupHandler();
        runtimeState.storeBackfillCorrelation('req-789', 'corr-789');

        await handlers['messaging-history.set']({
            syncType: 'ON_DEMAND',
            requestId: 'req-789',
            messages: [
                {
                    key: { id: 'm9', remoteJid: 'chat3@g.us', participant: 'z@s.whatsapp.net' },
                    message: { conversation: 'payload' },
                    messageTimestamp: 1700000020,
                },
            ],
            isLatest: false,
        });

        expect(producerMock.publish).toHaveBeenCalledWith('messages.history', expect.objectContaining({
            sync_type: 'ON_DEMAND',
            correlation_id: 'corr-789',
            messages: [expect.objectContaining({ message_id: 'm9' })],
        }));
    });

    it('registers stalled sync interval at 60 seconds', () => {
        const setIntervalSpy = jest.spyOn(global, 'setInterval');

        setupHandler();

        expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 60000);
        setIntervalSpy.mockRestore();
    });

    it('clears interval and saves watermarks on connection close', () => {
        const clearIntervalSpy = jest.spyOn(global, 'clearInterval').mockImplementation(() => undefined as any);
        const setIntervalSpy = jest.spyOn(global, 'setInterval').mockImplementation(() => 123 as any);

        setupHandler();

        handlers['connection.update']({ connection: 'close' });

        expect(clearIntervalSpy).toHaveBeenCalledWith(123);
        expect(fsMock.writeFileSync).toHaveBeenCalled();

        clearIntervalSpy.mockRestore();
        setIntervalSpy.mockRestore();
    });
});
