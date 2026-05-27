-- =====================================================================
-- Telegram collector Phase 1 schema fixes + new tables
-- Created: 2026-05-28
-- Purpose: fix broken telegram_chat_members, add reactions/polls/discussion-visits
-- =====================================================================

-- 1.1 — telegram_chat_members: drop broken bigint schema, recreate with UUID FKs
DROP TABLE IF EXISTS telegram_chat_members CASCADE;

CREATE TABLE telegram_chat_members (
    chat_id        UUID NOT NULL REFERENCES telegram_chats(id) ON DELETE CASCADE,
    user_id        UUID NOT NULL REFERENCES telegram_users(id) ON DELETE CASCADE,
    role           VARCHAR(32),               -- 'creator' / 'admin' / 'member' / 'restricted' / 'banned' / 'left'
    joined_at      TIMESTAMPTZ,
    last_seen_at   TIMESTAMPTZ,
    refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX idx_telegram_chat_members_user      ON telegram_chat_members (user_id);
CREATE INDEX idx_telegram_chat_members_refreshed ON telegram_chat_members (refreshed_at);

-- 1.2 — telegram_reactions: per-(message,user,emoji) reactor records
CREATE TABLE IF NOT EXISTS telegram_reactions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id     UUID NOT NULL REFERENCES telegram_messages(id) ON DELETE CASCADE,
    user_id        UUID REFERENCES telegram_users(id) ON DELETE SET NULL,
    emoji          VARCHAR(64) NOT NULL,
    is_big         BOOLEAN DEFAULT false,
    added_at       TIMESTAMPTZ,
    refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT unique_reaction_per_user UNIQUE (message_id, user_id, emoji)
);

CREATE INDEX IF NOT EXISTS idx_telegram_reactions_user    ON telegram_reactions (user_id);
CREATE INDEX IF NOT EXISTS idx_telegram_reactions_message ON telegram_reactions (message_id);
CREATE INDEX IF NOT EXISTS idx_telegram_reactions_emoji   ON telegram_reactions (emoji);

-- 1.3 — telegram_reaction_counts: per-message rollup (always populated even when reactor list capped)
CREATE TABLE IF NOT EXISTS telegram_reaction_counts (
    message_id        UUID PRIMARY KEY REFERENCES telegram_messages(id) ON DELETE CASCADE,
    counts            JSONB NOT NULL DEFAULT '{}'::jsonb,   -- { "👍": 142, "❤️": 87, ... }
    total_reactions   INTEGER NOT NULL DEFAULT 0,
    refreshed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telegram_reaction_counts_total ON telegram_reaction_counts (total_reactions DESC);

-- 1.4 — telegram_polls: full poll state including vote tallies
CREATE TABLE IF NOT EXISTS telegram_polls (
    message_id         UUID PRIMARY KEY REFERENCES telegram_messages(id) ON DELETE CASCADE,
    poll_id            VARCHAR(255) NOT NULL,
    question           TEXT,
    options            JSONB NOT NULL DEFAULT '[]'::jsonb,    -- [{"text": "Yes", "data": "0"}, ...]
    total_voters       INTEGER NOT NULL DEFAULT 0,
    vote_counts        JSONB NOT NULL DEFAULT '[]'::jsonb,    -- [{"option": 0, "voters": 142}, ...]
    is_closed          BOOLEAN DEFAULT false,
    is_anonymous       BOOLEAN DEFAULT true,
    allows_multiple    BOOLEAN DEFAULT false,
    quiz_correct_idx   INTEGER,                                -- for quiz polls
    refreshed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telegram_polls_poll_id ON telegram_polls (poll_id);

-- 1.5 — telegram_discussion_visits: anti-suspicion log of join→scrape→leave cycles
CREATE TABLE IF NOT EXISTS telegram_discussion_visits (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_chat_id       UUID NOT NULL REFERENCES telegram_chats(id) ON DELETE CASCADE,
    discussion_chat_id    UUID NOT NULL REFERENCES telegram_chats(id) ON DELETE CASCADE,
    account_name          VARCHAR(255) NOT NULL,
    joined_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at               TIMESTAMPTZ,
    members_collected     INTEGER NOT NULL DEFAULT 0,
    messages_collected    INTEGER NOT NULL DEFAULT 0,
    abort_reason          VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_telegram_discussion_visits_channel    ON telegram_discussion_visits (channel_chat_id, joined_at DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_discussion_visits_discussion ON telegram_discussion_visits (discussion_chat_id);
CREATE INDEX IF NOT EXISTS idx_telegram_discussion_visits_account    ON telegram_discussion_visits (account_name);
