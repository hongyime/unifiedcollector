import { getAuthState, handlePairingCode } from '../../services/wa-client-ts/src/auth_manager';
import * as baileys from '@whiskeysockets/baileys';

jest.mock('@whiskeysockets/baileys', () => ({
    useMultiFileAuthState: jest.fn()
}));

describe('Auth Manager', () => {
    beforeEach(() => {
        jest.clearAllMocks();
    });

    it('should get auth state successfully', async () => {
        const mockUseAuth = baileys.useMultiFileAuthState as jest.Mock;
        mockUseAuth.mockResolvedValue({
            state: { creds: {}, keys: {} },
            saveCreds: jest.fn()
        });

        const result = await getAuthState();

        expect(mockUseAuth).toHaveBeenCalled();
        expect(result).toHaveProperty('state');
        expect(result).toHaveProperty('saveCreds');
    });

    it('should ignore invalid phone number during pairing mode', async () => {
        const mockSock = {
            requestPairingCode: jest.fn()
        };

        await handlePairingCode(mockSock, 'invalid_phone');
        expect(mockSock.requestPairingCode).not.toHaveBeenCalled();
    });

    it('should call requestPairingCode with valid phone number', async () => {
        const mockSock = {
            requestPairingCode: jest.fn().mockResolvedValue('1234-5678')
        };

        await handlePairingCode(mockSock, '+1234567890');
        expect(mockSock.requestPairingCode).toHaveBeenCalledWith('1234567890');
    });
});
