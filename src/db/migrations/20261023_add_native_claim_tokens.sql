-- Lease fencing for browser_native_scrape_queue claims (GAP-4).
-- Every claim now issues a unique token; completion is compare-and-swap on the
-- token so an expired-lease worker can never complete (or fail) a row that a
-- newer worker has re-claimed. Existing rows are left NULL; the next claim
-- overwrites the token, and completion predicates treat NULL tokens as legacy
-- claims completed by status alone.
ALTER TABLE browser_native_scrape_queue ADD COLUMN IF NOT EXISTS claim_token UUID;
