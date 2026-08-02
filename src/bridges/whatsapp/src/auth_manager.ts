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

export async function requestPairingCode(sock: any, phone: string): Promise<string> {
    if (!phone.match(/^\+?[1-9]\d{1,14}$/)) {
        throw new Error('Invalid phone for pairing code. Use E.164 digits, for example +6591234567.');
    }
    const normalized = phone.replace(/[^0-9]/g, '');
    return await sock.requestPairingCode(normalized);
}

export async function handlePairingCode(sock: any, phone: string): Promise<void> {
    try {
        const code = await requestPairingCode(sock, phone);
        logger.info(`\n=========================\nPAIRING CODE: ${code}\n=========================\n`);
    } catch (err) {
        logger.error({ err }, 'Failed to request pairing code');
    }
}
