-- =====================================================================
-- Telegram user accounts table for bot-onboarded sessions
-- Created: 2026-05-28
-- Purpose: Store MTProto session strings for accounts onboarded via
--          /startcollector bot command or dashboard UI.
-- =====================================================================

CREATE TABLE IF NOT EXISTS telegram_user_accounts (
    name VARCHAR(64) PRIMARY KEY,           -- human-friendly name (e.g. "bryan_personal")
    api_id INTEGER NOT NULL,                 -- Telegram API ID
    api_hash VARCHAR(64) NOT NULL,           -- Telegram API hash
    phone VARCHAR(32) NOT NULL,              -- Phone number with country code
    session_string TEXT NOT NULL,            -- Telethon StringSession export
    owner_bot VARCHAR(64),                   -- Which bot onboarded this account
    status VARCHAR(20) DEFAULT 'active',     -- active, disabled, expired, banned
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_connected_at TIMESTAMPTZ,
    last_error TEXT,
    CONSTRAINT uq_telegram_user_accounts_phone UNIQUE (phone)
);

-- Index for status filtering during account loading
CREATE INDEX IF NOT EXISTS idx_telegram_user_accounts_status 
    ON telegram_user_accounts(status);

-- Notify channel for hot-reload (collector listens for new accounts)
-- Usage: after INSERT, call pg_notify('telegram_account_added', name)
-- The collector can LISTEN telegram_account_added and spawn a new worker.

COMMENT ON TABLE telegram_user_accounts IS 
    'MTProto user accounts onboarded via /startcollector bot or dashboard. '
    'Session strings allow reconnection without re-auth.';
COMMENT ON COLUMN telegram_user_accounts.session_string IS 
    'Telethon StringSession export. Contains auth key — treat as secret. '
    'Future: encrypt at rest with TELEGRAM_SESSION_KEY env var.';
