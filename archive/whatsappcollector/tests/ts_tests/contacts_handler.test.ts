jest.mock('../../services/wa-client-ts/src/producer', () => ({
    producer: {
        publish: jest.fn().mockResolvedValue(undefined),
    },
}));

jest.mock('../../services/wa-client-ts/src/profile_photo_fetcher', () => ({
    profilePhotoFetcher: {
        enqueue: jest.fn(),
    },
}));

import { registerContactsHandler } from '../../services/wa-client-ts/src/event_handlers/contacts';

const { producer } = require('../../services/wa-client-ts/src/producer');
const { profilePhotoFetcher } = require('../../services/wa-client-ts/src/profile_photo_fetcher');

describe('contacts handler utility consolidation coverage', () => {
    let handlers: Record<string, Function>;

    beforeEach(() => {
        handlers = {};
        (producer.publish as jest.Mock).mockClear();
        (profilePhotoFetcher.enqueue as jest.Mock).mockClear();
    });

    function createMockSocket() {
        return {
            ev: {
                on: jest.fn((event: string, handler: Function) => {
                    handlers[event] = handler;
                }),
            },
        } as any;
    }

    it('normalizes device suffix in phone JID and enqueues profile-photo fetch', async () => {
        const sock = createMockSocket();
        registerContactsHandler(sock);

        await handlers['contacts.update']([
            {
                id: '6588123456:12@s.whatsapp.net',
                notify: 'Alice',
            },
        ]);

        expect(producer.publish).toHaveBeenCalledWith('contacts.update', {
            jid: '6588123456@s.whatsapp.net',
            lid: null,
            display_name: 'Alice',
            phone_number: '6588123456',
        });
        expect(profilePhotoFetcher.enqueue).toHaveBeenCalledWith('6588123456@s.whatsapp.net');
    });

    it('maps LID-only contacts without enqueuing profile-photo fetch', async () => {
        const sock = createMockSocket();
        registerContactsHandler(sock);

        await handlers['contacts.upsert']([
            {
                id: '441122334455@lid',
                name: 'Masked User',
            },
        ]);

        expect(producer.publish).toHaveBeenCalledWith('contacts.update', {
            jid: null,
            lid: '441122334455@lid',
            display_name: 'Masked User',
            phone_number: null,
        });
        expect(profilePhotoFetcher.enqueue).not.toHaveBeenCalled();
    });
});
