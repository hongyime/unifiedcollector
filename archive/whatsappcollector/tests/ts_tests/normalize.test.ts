import { normalizeMessage } from '../../services/wa-client-ts/src/utils/normalize';

describe('normalizeMessage utility consolidation coverage', () => {
    it('normalizes group message and preserves LID participant mapping', () => {
        const normalized = normalizeMessage({
            key: {
                id: 'm-1',
                remoteJid: '123456789-111@g.us',
                participant: '441122334455@lid',
            },
            message: {
                extendedTextMessage: {
                    text: 'hello group',
                },
            },
            messageTimestamp: 1711111111,
        } as any);

        expect(normalized).not.toBeNull();
        expect(normalized?.chat_type).toBe('group');
        expect(normalized?.sender_jid).toBe('441122334455@lid');
        expect(normalized?.sender_lid).toBe('441122334455@lid');
        expect(normalized?.routing_key).toBe('msg.text');
        expect(normalized?.has_media).toBe(false);
    });

    it('normalizes newsletter messages as channel chat type', () => {
        const normalized = normalizeMessage({
            key: {
                id: 'm-2',
                remoteJid: '1203634@newsletter',
                participant: '1203634@newsletter',
            },
            message: {
                conversation: 'channel update',
            },
            messageTimestamp: 1711112222,
        } as any);

        expect(normalized).not.toBeNull();
        expect(normalized?.chat_type).toBe('channel');
        expect(normalized?.message_type).toBe('text');
        expect(normalized?.routing_key).toBe('msg.text');
    });

    it('routes status broadcast media to status channel contract', () => {
        const normalized = normalizeMessage({
            key: {
                id: 'm-3',
                remoteJid: 'status@broadcast',
                participant: '111@s.whatsapp.net',
            },
            message: {
                imageMessage: {
                    caption: 'story',
                    mimetype: 'image/jpeg',
                    directPath: '/story/abc',
                },
            },
            messageTimestamp: 1711113333,
        } as any);

        expect(normalized).not.toBeNull();
        expect(normalized?.chat_type).toBe('status');
        expect(normalized?.message_type).toBe('status');
        expect(normalized?.routing_key).toBe('msg.status');
        expect(normalized?.media_metadata).not.toBeNull();
        expect(normalized?.has_media).toBe(true);
    });

    it('skips reaction messages', () => {
        const normalized = normalizeMessage({
            key: {
                id: 'm-4',
                remoteJid: '123456789@s.whatsapp.net',
            },
            message: {
                reactionMessage: {
                    text: '👍',
                },
            },
            messageTimestamp: 1711114444,
        } as any);

        expect(normalized).toBeNull();
    });
});
