/**
 * Bug condition exploration test for BUG-2.
 *
 * FAILS on unfixed code (confirms bug). PASSES after fix.
 *
 * Validates: Requirements 2.1, 2.2, 2.4
 *
 * Bug condition: isBugCondition_B2(X) —
 *   FINDINGS_HUB_GROUP_JID is not set AND no group matches FINDINGS_HUB_GROUP_NAME
 *   → groups fetched but NOT published to broker
 *   → no findings_hub_configured event emitted
 */

import fs from 'fs';

// -------------------------------------------------------------------------
// Mock amqplib before any imports that use it
// -------------------------------------------------------------------------

const sendToQueueMock = jest.fn().mockReturnValue(true);
const checkQueueMock = jest.fn().mockResolvedValue(undefined);
const channelCloseMock = jest.fn().mockResolvedValue(undefined);
const connCloseMock = jest.fn().mockResolvedValue(undefined);
const connOnMock = jest.fn();

const mockChannel = {
    checkQueue: checkQueueMock,
    sendToQueue: sendToQueueMock,
    close: channelCloseMock,
    assertQueue: jest.fn().mockResolvedValue(undefined),
    consume: jest.fn(),
    prefetch: jest.fn().mockResolvedValue(undefined),
};

const mockConn = {
    createChannel: jest.fn().mockResolvedValue(mockChannel),
    close: connCloseMock,
    on: connOnMock,
};

jest.mock('amqplib', () => ({
    connect: jest.fn().mockResolvedValue(mockConn),
}));

jest.mock('ioredis', () => {
    return jest.fn().mockImplementation(() => ({
        connect: jest.fn().mockResolvedValue(undefined),
        xgroup: jest.fn().mockResolvedValue(undefined),
        xadd: jest.fn().mockResolvedValue(undefined),
        xreadgroup: jest.fn().mockResolvedValue(null),
        quit: jest.fn().mockResolvedValue(undefined),
    }));
});

// -------------------------------------------------------------------------
// Test data
// -------------------------------------------------------------------------

const MOCK_GROUPS = {
    'g1@g.us': { id: 'g1@g.us', subject: 'Group A', participants: [] },
    'g2@g.us': { id: 'g2@g.us', subject: 'Group B', participants: [] },
    'g3@g.us': { id: 'g3@g.us', subject: 'Group C', participants: [] },
};

// -------------------------------------------------------------------------
// BUG-2 exploration tests
// -------------------------------------------------------------------------

describe('BUG-2: FindingsHubSender group discovery — groups published and event emitted', () => {
    beforeEach(() => {
        jest.clearAllMocks();

        // Reset mock implementations after clearAllMocks
        sendToQueueMock.mockReturnValue(true);
        checkQueueMock.mockResolvedValue(undefined);
        channelCloseMock.mockResolvedValue(undefined);
        connCloseMock.mockResolvedValue(undefined);
        mockConn.createChannel.mockResolvedValue(mockChannel);

        // Ensure no JID override and no cache file
        delete process.env.FINDINGS_HUB_GROUP_JID;
        process.env.FINDINGS_HUB_ENABLED = 'true';
        process.env.FINDINGS_HUB_GROUP_NAME = 'Findings Hub'; // no match in MOCK_GROUPS
        process.env.BROKER_TYPE = 'rabbitmq';
        process.env.RABBITMQ_URL = 'amqp://localhost';
        process.env.SESSION_NAME = 'test-session';

        // Prevent cache file from short-circuiting group fetch
        jest.spyOn(fs, 'existsSync').mockReturnValue(false);
        jest.spyOn(fs, 'writeFileSync').mockImplementation(() => {});
    });

    afterEach(() => {
        jest.restoreAllMocks();
        jest.resetModules();
    });

    test('BUG-2 CONFIRMED: broker publish called with groups.metadata payload after detectHubGroup()', async () => {
        // Re-require for a fresh module instance
        jest.resetModules();

        // Re-apply mocks after resetModules
        jest.mock('amqplib', () => ({
            connect: jest.fn().mockResolvedValue(mockConn),
        }));
        jest.mock('ioredis', () => {
            return jest.fn().mockImplementation(() => ({
                connect: jest.fn().mockResolvedValue(undefined),
                xgroup: jest.fn().mockResolvedValue(undefined),
                xadd: jest.fn().mockResolvedValue(undefined),
                xreadgroup: jest.fn().mockResolvedValue(null),
                quit: jest.fn().mockResolvedValue(undefined),
            }));
        });

        const { FindingsHubSender } = require('../../services/wa-client-ts/src/findings_hub');
        const sender = new FindingsHubSender();

        const mockSock = {
            groupFetchAllParticipating: jest.fn().mockResolvedValue(MOCK_GROUPS),
            sendMessage: jest.fn().mockResolvedValue(undefined),
        };

        // Wire sock and call detectHubGroup directly (private method via cast)
        (sender as any).sock = mockSock;
        await (sender as any).detectHubGroup.call(sender);

        // On UNFIXED code: sendToQueue is never called with 'groups.metadata'
        // This assertion FAILS on unfixed code → confirms BUG-2 exists
        const groupMetadataCalls = sendToQueueMock.mock.calls.filter(
            (call: any[]) => call[0] === 'groups.metadata'
        );

        expect(groupMetadataCalls.length).toBeGreaterThan(0);
    });

    test('BUG-2 CONFIRMED: findings_hub_configured event published to session.events after JID resolved', async () => {
        jest.resetModules();

        jest.mock('amqplib', () => ({
            connect: jest.fn().mockResolvedValue(mockConn),
        }));
        jest.mock('ioredis', () => {
            return jest.fn().mockImplementation(() => ({
                connect: jest.fn().mockResolvedValue(undefined),
                xgroup: jest.fn().mockResolvedValue(undefined),
                xadd: jest.fn().mockResolvedValue(undefined),
                xreadgroup: jest.fn().mockResolvedValue(null),
                quit: jest.fn().mockResolvedValue(undefined),
            }));
        });

        // Use a group name that DOES match so JID gets resolved
        process.env.FINDINGS_HUB_GROUP_NAME = 'Group A';

        const { FindingsHubSender } = require('../../services/wa-client-ts/src/findings_hub');
        const sender = new FindingsHubSender();

        const mockSock = {
            groupFetchAllParticipating: jest.fn().mockResolvedValue(MOCK_GROUPS),
            sendMessage: jest.fn().mockResolvedValue(undefined),
        };

        (sender as any).sock = mockSock;
        await (sender as any).detectHubGroup.call(sender);

        // On UNFIXED code: no session.events publish with findings_hub_configured
        // This assertion FAILS on unfixed code → confirms BUG-2 exists
        const sessionEventCalls = sendToQueueMock.mock.calls.filter((call: any[]) => {
            if (call[0] !== 'session.events') return false;
            try {
                const payload = JSON.parse(call[1].toString());
                return payload.event_type === 'findings_hub_configured';
            } catch {
                return false;
            }
        });

        expect(sessionEventCalls.length).toBeGreaterThan(0);
    });
});

// -------------------------------------------------------------------------
// Preservation tests — JID override bypass
// These should PASS on both unfixed and fixed code
// -------------------------------------------------------------------------

describe('Preservation: FINDINGS_HUB_GROUP_JID override bypasses group fetch', () => {
    beforeEach(() => {
        jest.clearAllMocks();
        sendToQueueMock.mockReturnValue(true);
        checkQueueMock.mockResolvedValue(undefined);
        channelCloseMock.mockResolvedValue(undefined);
        connCloseMock.mockResolvedValue(undefined);
        mockConn.createChannel.mockResolvedValue(mockChannel);

        process.env.FINDINGS_HUB_ENABLED = 'true';
        process.env.BROKER_TYPE = 'rabbitmq';
        process.env.RABBITMQ_URL = 'amqp://localhost';
        process.env.SESSION_NAME = 'test-session';

        jest.spyOn(fs, 'existsSync').mockReturnValue(false);
        jest.spyOn(fs, 'writeFileSync').mockImplementation(() => {});
    });

    afterEach(() => {
        jest.restoreAllMocks();
        jest.resetModules();
    });

    test('Preservation: groupFetchAllParticipating NOT called when JID override is set', async () => {
        process.env.FINDINGS_HUB_GROUP_JID = '123456789@g.us';

        jest.resetModules();
        jest.mock('amqplib', () => ({ connect: jest.fn().mockResolvedValue(mockConn) }));
        jest.mock('ioredis', () => jest.fn().mockImplementation(() => ({
            connect: jest.fn(), xgroup: jest.fn(), xadd: jest.fn(),
            xreadgroup: jest.fn().mockResolvedValue(null), quit: jest.fn(),
        })));

        const { FindingsHubSender } = require('../../services/wa-client-ts/src/findings_hub');
        const sender = new FindingsHubSender();

        const groupFetchMock = jest.fn().mockResolvedValue({});
        const mockSock = {
            groupFetchAllParticipating: groupFetchMock,
            sendMessage: jest.fn(),
        };

        (sender as any).sock = mockSock;
        await (sender as any).detectHubGroup.call(sender);

        // JID override: groupFetchAllParticipating must NOT be called
        expect(groupFetchMock).not.toHaveBeenCalled();
        // JID must be set to the override value
        expect((sender as any).hubGroupJid).toBe('123456789@g.us');
    });

    test('Preservation: broker publish NOT called for group list when JID override is set', async () => {
        process.env.FINDINGS_HUB_GROUP_JID = '987654321@g.us';

        jest.resetModules();
        jest.mock('amqplib', () => ({ connect: jest.fn().mockResolvedValue(mockConn) }));
        jest.mock('ioredis', () => jest.fn().mockImplementation(() => ({
            connect: jest.fn(), xgroup: jest.fn(), xadd: jest.fn(),
            xreadgroup: jest.fn().mockResolvedValue(null), quit: jest.fn(),
        })));

        const { FindingsHubSender } = require('../../services/wa-client-ts/src/findings_hub');
        const sender = new FindingsHubSender();

        (sender as any).sock = { groupFetchAllParticipating: jest.fn(), sendMessage: jest.fn() };
        await (sender as any).detectHubGroup.call(sender);

        // No groups.metadata publish on override path
        const groupMetadataCalls = sendToQueueMock.mock.calls.filter(
            (call: any[]) => call[0] === 'groups.metadata'
        );
        expect(groupMetadataCalls.length).toBe(0);
    });
});
