// Normalize a Baileys WAMessage into the flat event shape the Python whatsapp
// collector consumes (src/collectors/whatsapp/__init__.py :: _handle_message_event).
//
// The consumer reads these fields (with fallbacks), so we emit them directly:
//   message_id, chat_jid, chat_name, pushName, session_name
//   messageType  -> RAW baileys type (e.g. 'imageMessage') used for has_media check
//   media_type   -> alias of messageType (consumer reads either)
//   body/text/caption, mediaKey (base64), directPath, media_url, mimetype
//   timestamp, sender_jid, chat_type, is_forwarded
//
// routing_key uses the 'messages.*' namespace so it matches the consumer's
// queue binding 'messages.#'. (The old standalone bridge used 'msg.*', which
// never routed to this collector -- that was a real bug.)

import { WAMessage } from '@whiskeysockets/baileys';

export interface NormalizedMessage {
    message_id: string | null | undefined;
    chat_jid: string;
    chat_type: string;
    chat_name: string;
    pushName: string | null;
    sender_jid: string;
    sender_lid: string | null;
    timestamp: number;
    message_type: string;       // canonical: text/image/video/...
    messageType: string;        // RAW baileys key: conversation/imageMessage/...
    media_type: string;         // alias of messageType for consumer's has_media check
    body: string;
    text: string;
    caption: string;
    has_media: boolean;
    mediaKey: string | null;
    directPath: string | null;
    media_url: string | null;
    mimetype: string | null;
    file_length: number | null;
    is_forwarded: boolean;
    forwarding_score: number;
    quoted_msg_id: string | null;
    location: {
        degreesLatitude: number | null;
        degreesLongitude: number | null;
        latitude: number | null;
        longitude: number | null;
        name: string | null;
        address: string | null;
        isLive: boolean;
        sequenceNumber: number | null;
    } | null;
    session_name: string;
    routing_key: string;
}

function toNumber(value: any): number | null {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim()) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
}

export function normalizeMessage(msg: WAMessage): NormalizedMessage | null {
    if (!msg.message || !msg.key) return null;

    const remoteJid = msg.key.remoteJid || '';
    const rawType = Object.keys(msg.message).find(
        (k) => k !== 'messageContextInfo' && k !== 'senderKeyDistributionMessage',
    ) || 'unknown';

    let body = '';
    let messageType = 'unknown';
    let routingKey = 'messages.unknown';
    const content: any = (msg.message as any)[rawType];

    if (rawType === 'conversation') {
        body = msg.message.conversation || '';
        messageType = 'text';
        routingKey = 'messages.text';
    } else if (rawType === 'extendedTextMessage') {
        body = msg.message.extendedTextMessage?.text || '';
        messageType = 'text';
        routingKey = 'messages.text';
    } else if (rawType === 'imageMessage') {
        messageType = 'image';
        routingKey = 'messages.media';
        body = content?.caption || '';
    } else if (rawType === 'videoMessage' || rawType === 'ptvMessage') {
        const isNote = content?.gifPlayback || (content?.seconds && content.seconds <= 60);
        messageType = isNote ? 'video_note' : 'video';
        routingKey = 'messages.media';
        body = content?.caption || '';
    } else if (rawType === 'audioMessage') {
        messageType = 'audio';
        routingKey = 'messages.media';
    } else if (rawType === 'documentMessage') {
        messageType = 'document';
        routingKey = 'messages.media';
        body = content?.title || content?.fileName || '';
    } else if (rawType === 'stickerMessage') {
        messageType = 'sticker';
        routingKey = 'messages.media';
    } else if (rawType === 'locationMessage' || rawType === 'liveLocationMessage') {
        messageType = rawType === 'liveLocationMessage' ? 'live_location' : 'location';
        routingKey = 'messages.location';
    } else if (rawType === 'reactionMessage') {
        return null; // skip reactions
    }

    let chatType = remoteJid.endsWith('@newsletter')
        ? 'channel'
        : remoteJid.includes('@g.us') ? 'group' : 'dm';

    if (remoteJid === 'status@broadcast') {
        routingKey = 'messages.status';
        messageType = 'status';
        chatType = 'status';
    }

    const contextInfo = content?.contextInfo || {};
    const participant = msg.key.participant || remoteJid || '';
    const senderLid = participant.includes('@lid') ? participant : null;

    let timestamp = msg.messageTimestamp as any;
    if (typeof timestamp !== 'number') {
        timestamp = timestamp?.low || Math.floor(Date.now() / 1000);
    }

    const hasMedia = ['image', 'video', 'video_note', 'audio', 'document', 'sticker'].includes(messageType);
    const lat = toNumber(content?.degreesLatitude ?? content?.latitude);
    const lon = toNumber(content?.degreesLongitude ?? content?.longitude);
    const isLocation = messageType === 'location' || messageType === 'live_location';
    const location = isLocation ? {
        degreesLatitude: lat,
        degreesLongitude: lon,
        latitude: lat,
        longitude: lon,
        name: content?.name || null,
        address: content?.address || null,
        isLive: messageType === 'live_location',
        sequenceNumber: toNumber(content?.sequenceNumber),
    } : null;

    // The consumer's has_media test compares against RAW baileys types
    // (imageMessage/videoMessage/...). Expose that raw type as messageType/media_type.
    const rawMediaType = hasMedia || isLocation ? rawType : (messageType === 'status' ? rawType : '');

    return {
        message_id: msg.key.id,
        chat_jid: remoteJid,
        chat_type: chatType,
        chat_name: (msg as any).pushName || remoteJid.split('@')[0],
        pushName: (msg as any).pushName || null,
        sender_jid: participant,
        sender_lid: senderLid,
        timestamp,
        message_type: messageType,
        messageType: rawMediaType,
        media_type: rawMediaType,
        body,
        text: body,
        caption: content?.caption || '',
        has_media: hasMedia,
        mediaKey: content?.mediaKey ? Buffer.from(content.mediaKey).toString('base64') : null,
        directPath: content?.directPath || null,
        media_url: content?.url || null,
        mimetype: content?.mimetype || null,
        file_length: content?.fileLength ? Number(content.fileLength) : null,
        is_forwarded: contextInfo?.isForwarded || false,
        forwarding_score: contextInfo?.forwardingScore || 0,
        quoted_msg_id: contextInfo?.stanzaId || null,
        location,
        session_name: process.env.SESSION_NAME || 'default',
        routing_key: routingKey,
    };
}
