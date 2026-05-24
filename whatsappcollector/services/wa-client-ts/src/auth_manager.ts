import { useMultiFileAuthState } from '@whiskeysockets/baileys';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export async function getAuthState() {
    const sessionName = process.env.SESSION_NAME || 'default';
    const authPath = process.env.AUTH_STORAGE_PATH || `./auth_info/${sessionName}`;
    logger.info(`Loading auth state for session '${sessionName}' from ${authPath}`);
    const { state, saveCreds } = await useMultiFileAuthState(authPath);
    return { state, saveCreds };
}

export async function handlePairingCode(sock: any, phone: string) {
    if (!phone.match(/^\+?[1-9]\d{1,14}$/)) {
        logger.error(`Invalid phone number format for pairing code: ${phone}. Must be E.164 format.`);
        return;
    }

    try {
        logger.info(`Requesting pairing code for phone number: ${phone}`);
        const normalizedPhone = phone.replace(/[^0-9]/g, '');
        const code = await sock.requestPairingCode(normalizedPhone);
        logger.info(`\n=========================\nPAIRING CODE: ${code}\n=========================\n`);
    } catch (err) {
        logger.error({ err }, 'Failed to request pairing code');
    }
}
