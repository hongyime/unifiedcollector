-- 2026-07-04: collected_at/ingested_at indexes on the messaging tables.
-- max(collected_at) on telegram_messages (747k rows, no index) was a 41s seq scan —
-- run every tick by the freshness watchdog, the scheduler heartbeat, and the new
-- dashboard /collectors/live endpoint (where the 41s blew the timeout and telegram
-- showed as 'unknown'). A btree makes each max() an instant index scan.
-- Built CONCURRENTLY out-of-band on the live DB (these are hot realtime tables);
-- plain form here for clean-rebuild parity (IF NOT EXISTS = no-op where present).
CREATE INDEX IF NOT EXISTS idx_telegram_messages_collected ON telegram_messages(collected_at);
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_collected ON whatsapp_messages(collected_at);
CREATE INDEX IF NOT EXISTS idx_beeper_shadow_ingested      ON beeper_shadow_messages(ingested_at);
