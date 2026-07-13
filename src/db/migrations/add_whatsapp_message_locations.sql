-- 2026-07-13: Tier 5 shared/live-location capture for whatsapp (mirrors
-- telegram_message_locations). WhatsApp locationMessage / liveLocationMessage
-- carry degreesLatitude/degreesLongitude; extract them into a structured table.
-- chat_jid is text (whatsapp's platform id) rather than a UUID FK.
CREATE TABLE IF NOT EXISTS whatsapp_message_locations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_message_id text NOT NULL,
    chat_jid            text,
    latitude            double precision NOT NULL,
    longitude           double precision NOT NULL,
    is_live             boolean NOT NULL DEFAULT false,
    name                text,
    address             text,
    collected_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform_message_id)
);
CREATE INDEX IF NOT EXISTS idx_wa_msg_loc_chat ON whatsapp_message_locations (chat_jid);
