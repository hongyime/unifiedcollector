// Baileys multi-file auth state. The session lives in AUTH_STORAGE_PATH
// (mounted from the host at sessions/whatsapp/<account>). Keeping these files
// intact across restarts/migrations is what preserves the WhatsApp link.

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

export async function handlePairingCode(sock: any, phone: string): Promise<void> {
    if (!phone.match(/^\+?[1-9]\d{1,14}$/)) {
        logger.error(`Invalid phone for pairing code: ${phone}. Must be E.164.`);
        return;
    }
    try {
        const normalized = phone.replace(/[^0-9]/g, '');
        const code = await sock.requestPairingCode(normalized);
        logger.info(`\n=========================\nPAIRING CODE: ${code}\n=========================\n`);
    } catch (err) {
        logger.error({ err }, 'Failed to request pairing code');
    }
}
