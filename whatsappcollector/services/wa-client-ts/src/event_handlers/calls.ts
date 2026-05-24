import { WASocket } from '@whiskeysockets/baileys';
import { producer } from '../producer';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export function registerCallsHandler(sock: WASocket) {
    sock.ev.on('call', async (calls) => {
        try {
            for (const call of calls) {
                const payload = {
                    call_id: call.id,
                    from: call.from,
                    date: call.date ? call.date.toISOString() : new Date().toISOString(),
                    status: call.status,
                    is_video: call.isVideo || false,
                    is_group: call.isGroup || false,
                    offline: call.offline || false,
                };

                logger.info({
                    call_id: call.id,
                    from: call.from,
                    status: call.status,
                    isVideo: call.isVideo,
                }, 'Call event received');

                await producer.publish('session.status', {
                    event_type: 'call',
                    session_name: process.env.SESSION_NAME || 'default',
                    ...payload,
                });
            }
        } catch (err) {
            logger.error({ err }, 'Error in call handler');
        }
    });
}
