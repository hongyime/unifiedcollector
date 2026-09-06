-- DATA-004 backfill: Telegram enforces that bot usernames end in "bot".
-- Historical telegram_users rows written before the is_bot column existed sit
-- with is_bot=NULL; new rows correctly carry the value via _upsert_user_full.
-- This one-shot backfill uses the "%bot" suffix — a Telegram-enforced invariant,
-- not a heuristic — to promote the historical NULL rows for bots to is_bot=true.
--
-- Non-bot users cannot have a "%bot"-suffixed username (Telegram rejects the
-- registration), so there is no false-positive risk. The remaining NULL rows
-- are legitimate humans that predate the column; leave them NULL so the
-- analyzer's `is_bot IS NOT TRUE` filter still lets them through entity
-- resolution.
--
-- Idempotent: only touches rows where is_bot IS NULL.

UPDATE telegram_users
SET is_bot = true, updated_at = NOW()
WHERE is_bot IS NULL
  AND username IS NOT NULL
  AND lower(username) LIKE '%bot';
