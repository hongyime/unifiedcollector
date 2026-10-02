-- ARCHIVED 2026-10-02 (W10, Postgres split): moved out of the auto-applied
-- schemas/ set. schemas/*.sql is globbed non-recursively, so a file under
-- _archive/ is never applied on boot. The wa_face_* tables are analyzer-
-- destined (face work lives in unifiedanalyzer's facetracker schema now);
-- drop them from the live collector DB with migrations/_archive/
-- drop_wa_face_tables.sql. Kept here for history only.
--
-- Face Recognition tables (Phase 2: will be generalized to platform-agnostic
-- face_identities / face_embeddings owned by unifiedanalyzer).
-- Currently WA-specific. Not auto-applied — requires pgvector extension.

CREATE TABLE IF NOT EXISTS wa_face_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label TEXT UNIQUE,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wa_face_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id UUID REFERENCES wa_face_identities(id) ON DELETE SET NULL,
    embedding vector(128),
    source_media_id UUID REFERENCES media_items(id) ON DELETE CASCADE,
    face_box JSONB, -- [top, right, bottom, left]
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wa_face_identity ON wa_face_embeddings(identity_id);
