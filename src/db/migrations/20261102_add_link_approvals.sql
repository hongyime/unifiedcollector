-- Reviewer-gated messaging link workflow. A collector may act only on an
-- account-scoped approval row, never directly on a discovered link.
CREATE TABLE IF NOT EXISTS link_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source VARCHAR(50) NOT NULL,
    source_link_id INTEGER REFERENCES discovered_links(id) ON DELETE CASCADE,
    target_url TEXT NOT NULL,
    account_name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    requested_by BIGINT,
    approved_by BIGINT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days',
    last_error TEXT,
    control_card_token TEXT UNIQUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT link_approvals_state_check CHECK (state IN (
        'pending', 'approved', 'joining', 'joined', 'collection_active',
        'rejected', 'failed', 'expired', 'cooldown_deferred'
    )),
    CONSTRAINT link_approvals_source_account_unique UNIQUE (source, source_link_id, account_name)
);

CREATE INDEX IF NOT EXISTS idx_link_approvals_ready
    ON link_approvals (source, state, expires_at, requested_at DESC);
