CREATE TABLE IF NOT EXISTS telegram_account_throughput (
    account_identity TEXT PRIMARY KEY,
    claim_budget INTEGER NOT NULL DEFAULT 1,
    active_claims INTEGER NOT NULL DEFAULT 0,
    claims_done BIGINT NOT NULL DEFAULT 0,
    written_messages BIGINT NOT NULL DEFAULT 0,
    write_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    flood_waits BIGINT NOT NULL DEFAULT 0,
    cooldown_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
