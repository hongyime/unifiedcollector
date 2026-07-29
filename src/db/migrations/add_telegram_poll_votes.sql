-- Store per-user Telegram poll votes when Telegram exposes voters
-- (public/non-anonymous polls). Anonymous polls still only expose counts.

CREATE TABLE IF NOT EXISTS telegram_poll_votes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      UUID NOT NULL REFERENCES telegram_messages(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES telegram_users(id) ON DELETE CASCADE,
    option_indices  JSONB NOT NULL DEFAULT '[]'::jsonb,
    option_data     JSONB NOT NULL DEFAULT '[]'::jsonb,
    voted_at        TIMESTAMPTZ,
    refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT unique_telegram_poll_vote_per_user UNIQUE (message_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_telegram_poll_votes_message
    ON telegram_poll_votes (message_id);

CREATE INDEX IF NOT EXISTS idx_telegram_poll_votes_user
    ON telegram_poll_votes (user_id);
