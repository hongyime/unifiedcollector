type QrStatus = 'waiting' | 'scanned' | 'connected' | 'error';

export interface QrSnapshot {
    status: QrStatus;
    qr: string | null;
    session_name: string;
    error: string | null;
}

interface BackfillCorrelationEntry {
    correlationId: string;
    createdAt: number;
}

const CORRELATION_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_JOINS_PER_HOUR_DEFAULT = 3;
const JOIN_WINDOW_MS = 60 * 60 * 1000;

let qrSnapshot: QrSnapshot = {
    status: 'waiting',
    qr: null,
    session_name: process.env.SESSION_NAME || 'default',
    error: null,
};

const backfillCorrelations = new Map<string, BackfillCorrelationEntry>();
const joinAttemptsBySession = new Map<string, number[]>();

export function setQrWaiting(sessionName: string, qr: string) {
    qrSnapshot = {
        status: 'waiting',
        qr,
        session_name: sessionName,
        error: null,
    };
}

export function setQrScanned(sessionName: string) {
    qrSnapshot = {
        ...qrSnapshot,
        status: 'scanned',
        session_name: sessionName,
        error: null,
    };
}

export function setQrConnected(sessionName: string) {
    qrSnapshot = {
        status: 'connected',
        qr: null,
        session_name: sessionName,
        error: null,
    };
}

export function setQrError(sessionName: string, error: string) {
    qrSnapshot = {
        status: 'error',
        qr: null,
        session_name: sessionName,
        error,
    };
}

export function getQrSnapshot(sessionName?: string): QrSnapshot {
    if (sessionName && qrSnapshot.session_name !== sessionName) {
        return {
            status: 'waiting',
            qr: null,
            session_name: sessionName,
            error: null,
        };
    }

    return qrSnapshot;
}

function cleanupExpiredCorrelations(now = Date.now()) {
    for (const [requestId, entry] of backfillCorrelations.entries()) {
        if (now - entry.createdAt > CORRELATION_TTL_MS) {
            backfillCorrelations.delete(requestId);
        }
    }
}

export function storeBackfillCorrelation(requestId: string, correlationId: string) {
    cleanupExpiredCorrelations();
    backfillCorrelations.set(requestId, {
        correlationId,
        createdAt: Date.now(),
    });
}

export function getBackfillCorrelation(requestId: string): string | null {
    cleanupExpiredCorrelations();
    return backfillCorrelations.get(requestId)?.correlationId || null;
}

export function deleteBackfillCorrelation(requestId: string) {
    backfillCorrelations.delete(requestId);
}

export function recordJoinAttempt(sessionName: string) {
    const now = Date.now();
    const maxJoins = Number(process.env.LINK_DISCOVERY_MAX_JOINS_PER_HOUR || MAX_JOINS_PER_HOUR_DEFAULT);
    const attempts = joinAttemptsBySession.get(sessionName) || [];
    const recentAttempts = attempts.filter(timestamp => now - timestamp < JOIN_WINDOW_MS);

    if (recentAttempts.length >= maxJoins) {
        const oldestRecent = Math.min(...recentAttempts);
        const retryAfterMs = Math.max(JOIN_WINDOW_MS - (now - oldestRecent), 0);
        joinAttemptsBySession.set(sessionName, recentAttempts);
        return {
            allowed: false,
            retryAfterSeconds: Math.max(1, Math.ceil(retryAfterMs / 1000)),
            attemptsInWindow: recentAttempts.length,
            limit: maxJoins,
        };
    }

    recentAttempts.push(now);
    joinAttemptsBySession.set(sessionName, recentAttempts);

    return {
        allowed: true,
        retryAfterSeconds: 0,
        attemptsInWindow: recentAttempts.length,
        limit: maxJoins,
    };
}

export function resetJoinAttempts(sessionName?: string) {
    if (sessionName) {
        joinAttemptsBySession.delete(sessionName);
        return;
    }
    joinAttemptsBySession.clear();
}

export function resetRuntimeState(sessionName = process.env.SESSION_NAME || 'default') {
    qrSnapshot = {
        status: 'waiting',
        qr: null,
        session_name: sessionName,
        error: null,
    };
    backfillCorrelations.clear();
    joinAttemptsBySession.clear();
}
