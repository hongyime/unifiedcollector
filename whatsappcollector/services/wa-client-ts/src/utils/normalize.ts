import { WAMessage } from '@whiskeysockets/baileys';

export function normalizeMessage(msg: WAMessage) {
    if (!msg.message || !msg.key) return null;

    const remoteJid = msg.key.remoteJid || '';

    let msgType = Object.keys(msg.message).find(k => k !== 'messageContextInfo' && k !== 'senderKeyDistributionMessage') || 'unknown';

    let body = '';
    let messageType = 'unknown';
    let routingKey = 'msg.unknown';
    let content: any = (msg.message as any)[msgType];

    if (msgType === 'conversation') {
        body = msg.message.conversation || '';
        messageType = 'text';
        routingKey = 'msg.text';
    } else if (msgType === 'extendedTextMessage') {
        body = msg.message.extendedTextMessage?.text || '';
        messageType = 'text';
        routingKey = 'msg.text';
    } else if (msgType === 'imageMessage') {
        messageType = 'image';
        routingKey = 'msg.media.image';
        body = content?.caption || '';
    } else if (msgType === 'videoMessage') {
        const isVideoNote = content?.gifPlayback || (content?.seconds && content.seconds <= 60);
        messageType = isVideoNote ? 'video_note' : 'video';
        routingKey = 'msg.media.video';
        body = content?.caption || '';
    } else if (msgType === 'ptvMessage') {
        messageType = 'video_note';
        routingKey = 'msg.media.video';
        body = content?.caption || '';
    } else if (msgType === 'audioMessage') {
        messageType = 'audio';
        routingKey = 'msg.media.audio';
    } else if (msgType === 'documentMessage') {
        messageType = 'document';
        routingKey = 'msg.media.document';
        body = content?.title || content?.fileName || '';
    } else if (msgType === 'stickerMessage') {
        messageType = 'sticker';
        routingKey = 'msg.media.sticker';
    } else if (msgType === 'reactionMessage') {
        // Skip explicitly
        return null;
    }

    let chatType = remoteJid.endsWith('@newsletter') ? 'channel' : (remoteJid.includes('@g.us') ? 'group' : 'dm');

    if (remoteJid === 'status@broadcast') {
        routingKey = 'msg.status';
        messageType = 'status';
        chatType = 'status';
    }

    const contextInfo = content?.contextInfo || {};

    const participant = msg.key.participant || remoteJid || '';
    let sender_lid = null;
    let sender_jid = participant;
    if (participant.includes('@lid')) {
        sender_lid = participant;
    }

    let timestamp = msg.messageTimestamp;
    if (typeof timestamp !== 'number') {
        timestamp = (timestamp as any)?.low || Math.floor(Date.now() / 1000);
    }

    const hasMedia = ['image', 'video', 'video_note', 'audio', 'document', 'sticker'].includes(messageType)
        || (messageType === 'status' && msgType.endsWith('Message'));

    return {
        message_id: msg.key.id,
        chat_jid: remoteJid,
        chat_type: chatType,
        sender_jid: sender_jid,
        sender_lid: sender_lid,
        timestamp: timestamp,
        message_type: messageType,
        body: body,
        is_forwarded: contextInfo?.isForwarded || false,
        forwarding_score: contextInfo?.forwardingScore || 0,
        quoted_msg_id: contextInfo?.stanzaId || null,
        session_name: process.env.SESSION_NAME || 'default',
        media_metadata: hasMedia ? {
            mediaKey: content?.mediaKey ? Buffer.from(content.mediaKey).toString('base64') : null,
            directPath: content?.directPath || null,
            url: content?.url || null,
            mimetype: content?.mimetype || null,
            fileLength: content?.fileLength ? Number(content.fileLength) : null
        } : null,
        has_media: hasMedia,
        routing_key: routingKey,
        // Store only the structural envelope needed for media re-download.
        // Strips large binary fields (thumbnails, mediaKey buffers) to keep DB lean.
        raw_payload: {
            key: msg.key,
            message: msg.message,
            messageTimestamp: msg.messageTimestamp,
            pushName: (msg as any).pushName || null,
            broadcast: (msg as any).broadcast || false,
        }
    };
}
