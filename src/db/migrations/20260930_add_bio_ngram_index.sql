-- 2026-09-30: distinctive bio n-grams (5-8 word phrases with low DF) mapped
-- to the platform users that carry them. Consumed by
-- src/pipeline/bio_clustering.py to detect coordinated fake-account networks
-- (LinkedIn-fakes pattern, Griffin's 2021-11-16 post) across every platform
-- we already collect bios from.
--
-- Storage cost bound: bounded batches per cycle; nightly GC drops
-- singletons older than 180d. Estimated max ~100MB steady-state.

CREATE TABLE IF NOT EXISTS bio_ngram_index (
    ngram_hash      bytea       NOT NULL,       -- BLAKE2b-64 of normalized ngram
    ngram_text      text        NOT NULL,       -- kept for readability & debugging
    ngram_length    smallint    NOT NULL,       -- word count 5-8
    platform        text        NOT NULL,       -- 'telegram' | 'instagram' | ...
    platform_uid    text        NOT NULL,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ngram_hash, platform, platform_uid)
);

CREATE INDEX IF NOT EXISTS idx_bio_ngram_index_hash
    ON bio_ngram_index(ngram_hash);
CREATE INDEX IF NOT EXISTS idx_bio_ngram_index_platform
    ON bio_ngram_index(platform, platform_uid);
CREATE INDEX IF NOT EXISTS idx_bio_ngram_index_first_seen
    ON bio_ngram_index(first_seen);

-- Optional operator-maintained denylist for legitimate shared phrases
-- (song lyrics, famous bios) that keep clustering falsely.
CREATE TABLE IF NOT EXISTS bio_ngram_denylist (
    ngram_hash      bytea       PRIMARY KEY,
    ngram_text      text        NOT NULL,
    reason          text        NULL,
    added_at        timestamptz NOT NULL DEFAULT now()
);
