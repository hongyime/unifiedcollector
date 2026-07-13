-- 2026-07-13: Tier 5 shared/live-location capture for telegram.
-- Location messages (MessageMediaGeo / GeoLive / Venue) carry lat/long that were
-- preserved only inside telegram_messages.metadata (raw dict) — not queryable.
-- Extract them into a dedicated table, keyed by the message's platform id, so the
-- analyzer/dashboard can map shared locations. Populated by a bounded per-cycle
-- backfill (TelegramCollector._backfill_message_locations) that reads the geo out
-- of existing metadata — never touches the hot message INSERT path.
CREATE TABLE IF NOT EXISTS telegram_message_locations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_message_id text NOT NULL,
    chat_id             uuid REFERENCES telegram_chats(id),
    latitude            double precision NOT NULL,
    longitude           double precision NOT NULL,
    is_live             boolean NOT NULL DEFAULT false,
    venue_title         text,
    venue_address       text,
    collected_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform_message_id)
);
CREATE INDEX IF NOT EXISTS idx_tg_msg_loc_chat ON telegram_message_locations (chat_id);

-- Partial index over ONLY the geo-bearing messages (a tiny fraction of the ~1.2M
-- rows) so the backfill's "find location messages not yet extracted" scan is
-- index-backed instead of a full seq scan every cycle.
CREATE INDEX IF NOT EXISTS idx_tg_messages_geo
    ON telegram_messages (collected_at DESC)
    WHERE (metadata -> 'media' -> 'geo' ->> 'lat') IS NOT NULL;
