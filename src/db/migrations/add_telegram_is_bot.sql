-- SYNC #40: flag telegram bots so the analyzer can exclude non-humans from
-- entity creation. Bots (and channels appearing as users) are not people; a
-- shared bot contact is false identity evidence that can merge unrelated people.
-- Nullable boolean = instant, no table rewrite. IF NOT EXISTS keeps it idempotent
-- (the runner also applies it manually-first; re-apply is a no-op).
ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS is_bot boolean;
