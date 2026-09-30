-- 2026-09-30: GAN-face synthetic-avatar score columns on entity_faces
-- (Do Next #4). NULL = never scored; 0.0 = clearly real; 1.0 = clearly
-- GAN-generated. Only populated for faces classified by GanDetector;
-- entity_faces from other methods keep NULL.
--
-- Griffin's "LinkedIn Fakes" (2021-11-16) documented ~300 profiles all
-- sharing StyleGAN2 tells - we want an offline classifier and a
-- per-face score so the analyzer can weight synthetic pairs as
-- negative-evidence signals rather than positive-evidence merges.
ALTER TABLE entity_faces
    ADD COLUMN IF NOT EXISTS gan_score REAL NULL,
    ADD COLUMN IF NOT EXISTS gan_model_version TEXT NULL,
    ADD COLUMN IF NOT EXISTS gan_scored_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_entity_faces_gan_score_high
    ON entity_faces(gan_score DESC)
    WHERE gan_score IS NOT NULL AND gan_score > 0.5;

-- Operator override table: manually mark a face as real/synthetic
-- after review. Screening pipeline respects overrides on re-score.
CREATE TABLE IF NOT EXISTS face_gan_overrides (
    face_id       INTEGER     PRIMARY KEY,
    is_synthetic  BOOLEAN     NOT NULL,
    reviewed_by   TEXT        NULL,
    reviewed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    note          TEXT        NULL
);
