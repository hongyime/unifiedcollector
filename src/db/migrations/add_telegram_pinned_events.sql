-- 2026-07-13: Tier 6 pinned-message + event/RSVP capture for telegram.
-- (a) is_pinned: Telethon exposes message.pinned (bool) — persist it on
--     telegram_messages so the dashboard can surface pinned messages per chat.
-- (b) telegram_events: venue/event info (MessageMediaVenue title/address/
--     venue_type, pin-service actions). Geo lat/lng is intentionally NOT stored
--     here — location extraction lives in telegram_message_locations (Tier 5).
-- Written by TelegramCollector._write_realtime_message / _upsert_message and
-- the best-effort _extract_message_event helper.
ALTER TABLE telegram_messages
    ADD COLUMN IF NOT EXISTS is_pinned boolean NOT NULL DEFAULT false;

-- Partial index: almost all of the ~1.2M rows are is_pinned = false, so the
-- index only ever contains the handful of pinned rows — cheap to build & keep.
CREATE INDEX IF NOT EXISTS idx_tg_messages_pinned
    ON telegram_messages (chat_id)
    WHERE is_pinned;

CREATE TABLE IF NOT EXISTS telegram_events (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_message_id text UNIQUE NOT NULL,
    chat_id             uuid REFERENCES telegram_chats(id),
    event_type          text,   -- 'venue' | 'pin' | ...
    title               text,
    address             text,
    venue_type          text,
    starts_at           timestamptz,
    collected_at        timestamptz NOT NULL DEFAULT now()
);
