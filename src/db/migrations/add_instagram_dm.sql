-- 2026-07-05: Instagram DMs, captured ban-safely by the extension OBSERVING the
-- direct_v2 responses the page already fetches (no extra requests). Per-account:
-- is_from_me is set by comparing sender to the logged-in owner (ds_user_id).
CREATE TABLE IF NOT EXISTS instagram_dm_thread (
    thread_id    text PRIMARY KEY,
    title        text,
    participants text[],
    owner_account text,
    last_activity timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS instagram_dm (
    message_id   text PRIMARY KEY,
    thread_id    text,
    sender_id    text,
    sender_username text,
    text         text,
    item_type    text,
    "timestamp"  timestamptz,
    is_from_me   boolean NOT NULL DEFAULT false,
    owner_account text,
    collected_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ig_dm_thread ON instagram_dm(thread_id, "timestamp");
CREATE INDEX IF NOT EXISTS idx_ig_dm_owner ON instagram_dm(owner_account);
