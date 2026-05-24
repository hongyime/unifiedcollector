/**
 * Tests for FindingsHubSender JID file cache persistence: BUG-12.
 */
import fs from 'fs';
import path from 'path';

// We test the detectHubGroup private method behaviour by exercising it via start()
// using mocked fs and socket.

const JID_CACHE_PATH = '/app/auth_info/findings_hub_jid.txt';

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------

function makeMockSock(groups: Record<string, { subject: string; id: string }> = {}) {
    return {
        groupFetchAllParticipating: jest.fn().mockResolvedValue(groups),
        sendMessage: jest.fn().mockResolvedValue(undefined),
    };
}

function makeInstance() {
    // Re-require to get a fresh instance with no cached state
    jest.resetModules();
    const mod = require('../../services/wa-client-ts/src/findings_hub');
    return new mod.FindingsHubSender();
}

// -------------------------------------------------------------------------
// Unit: API called only once when JID file is written after first detection
// -------------------------------------------------------------------------

describe('JID cache: write after detection', () => {
    beforeEach(() => {
        jest.clearAllMocks();
        delete process.env.FINDINGS_HUB_GROUP_JID;
        process.env.FINDINGS_HUB_ENABLED = 'true';
        process.env.FINDINGS_HUB_GROUP_NAME = 'Test Hub Group';
        process.env.BROKER_TYPE = 'rabbitmq';
    });

    it('writes JID to cache file after detecting via API', async () => {
        const writeSpy = jest.spyOn(fs, 'writeFileSync').mockImplementation(() => {});
        jest.spyOn(fs, 'existsSync').mockReturnValue(false);
        jest.spyOn(fs, 'readFileSync');

        const groups = { 'group1': { subject: 'Test Hub Group', id: '111@g.us' } };
        const sock = makeMockSock(groups);

        const { FindingsHubSender } = require('../../services/wa-client-ts/src/findings_hub');
        const sender = new FindingsHubSender();

        // Access private method via cast
        await (sender as any).detectHubGroup.call(sender);
        // Internally calls sock which is not wired yet — wire it manually
        (sender as any).sock = sock;
        await (sender as any).detectHubGroup.call(sender);

        expect(writeSpy).toHaveBeenCalledWith(
            JID_CACHE_PATH,
            expect.stringContaining('111@g.us'),
            'utf8'
        );

        writeSpy.mockRestore();
    });
});

// -------------------------------------------------------------------------
// Unit: JID file exists — groupFetchAllParticipating NOT called
// -------------------------------------------------------------------------

describe('JID cache: load from file', () => {
    beforeEach(() => {
        jest.clearAllMocks();
        delete process.env.FINDINGS_HUB_GROUP_JID;
        process.env.FINDINGS_HUB_ENABLED = 'true';
        process.env.FINDINGS_HUB_GROUP_NAME = 'Test Hub Group';
        process.env.BROKER_TYPE = 'rabbitmq';
    });

    it('uses cached JID and skips groupFetchAllParticipating when cache file exists', async () => {
        jest.spyOn(fs, 'existsSync').mockReturnValue(true);
        jest.spyOn(fs, 'readFileSync').mockReturnValue('222@g.us');

        const sock = makeMockSock();

        const { FindingsHubSender } = require('../../services/wa-client-ts/src/findings_hub');
        const sender = new FindingsHubSender();
        (sender as any).sock = sock;

        await (sender as any).detectHubGroup.call(sender);

        expect(sock.groupFetchAllParticipating).not.toHaveBeenCalled();
        expect((sender as any).hubGroupJid).toBe('222@g.us');

        (fs.existsSync as jest.Mock).mockRestore();
        (fs.readFileSync as jest.Mock).mockRestore();
    });
});

// -------------------------------------------------------------------------
// Unit: FINDINGS_HUB_GROUP_JID env var set — API not called (existing behaviour)
// -------------------------------------------------------------------------

describe('JID cache: env var override takes priority', () => {
    beforeEach(() => {
        jest.clearAllMocks();
        process.env.FINDINGS_HUB_GROUP_JID = '333@g.us';
        process.env.FINDINGS_HUB_ENABLED = 'true';
        process.env.FINDINGS_HUB_GROUP_NAME = 'Test Hub Group';
        process.env.BROKER_TYPE = 'rabbitmq';
    });

    afterEach(() => {
        delete process.env.FINDINGS_HUB_GROUP_JID;
    });

    it('does not call groupFetchAllParticipating when env var is set', async () => {
        const existsSpy = jest.spyOn(fs, 'existsSync');
        const sock = makeMockSock();

        const { FindingsHubSender } = require('../../services/wa-client-ts/src/findings_hub');
        const sender = new FindingsHubSender();
        (sender as any).sock = sock;

        await (sender as any).detectHubGroup.call(sender);

        expect(sock.groupFetchAllParticipating).not.toHaveBeenCalled();
        expect((sender as any).hubGroupJid).toBe('333@g.us');

        existsSpy.mockRestore();
    });
});

// -------------------------------------------------------------------------
// Property test: with FINDINGS_HUB_GROUP_JID set, API never called on restart
// -------------------------------------------------------------------------

describe('JID cache: property — env var prevents API calls on any restart', () => {
    beforeEach(() => {
        jest.clearAllMocks();
        process.env.FINDINGS_HUB_ENABLED = 'true';
        process.env.BROKER_TYPE = 'rabbitmq';
    });

    afterEach(() => {
        delete process.env.FINDINGS_HUB_GROUP_JID;
    });

    // Simulate N restarts with FINDINGS_HUB_GROUP_JID always set
    const restartCounts = [1, 2, 5, 10, 20];

    restartCounts.forEach(restarts => {
        it(`groupFetchAllParticipating is never called across ${restarts} restarts with env override`, async () => {
            process.env.FINDINGS_HUB_GROUP_JID = '444@g.us';

            for (let i = 0; i < restarts; i++) {
                jest.resetModules();
                const sock = makeMockSock();
                const { FindingsHubSender } = require('../../services/wa-client-ts/src/findings_hub');
                const sender = new FindingsHubSender();
                (sender as any).sock = sock;

                await (sender as any).detectHubGroup.call(sender);

                expect(sock.groupFetchAllParticipating).not.toHaveBeenCalled();
            }
        });
    });
});
