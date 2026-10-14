-- Grow search from newly collected public profiles/posts and expose the
-- document index under the requested canonical table name.
ALTER TABLE search_dynamic_terms
    ADD COLUMN IF NOT EXISTS last_result_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_result_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS zero_result_since TIMESTAMPTZ;

ALTER TABLE search_dynamic_terms
    DROP CONSTRAINT IF EXISTS search_dynamic_terms_term_kind_check,
    DROP CONSTRAINT IF EXISTS search_dynamic_terms_template_cursor_check;

ALTER TABLE search_dynamic_terms
    ADD CONSTRAINT search_dynamic_terms_term_kind_check
        CHECK (term_kind IN ('username', 'full_name', 'keyword', 'hashtag')),
    ADD CONSTRAINT search_dynamic_terms_template_cursor_check
        CHECK (template_cursor BETWEEN 0 AND 31),
    ADD CONSTRAINT search_dynamic_terms_last_result_count_check
        CHECK (last_result_count >= 0),
    ADD CONSTRAINT search_dynamic_terms_total_result_count_check
        CHECK (total_result_count >= 0);

DO $$
BEGIN
    IF to_regclass('public.search_documents') IS NULL
       AND to_regclass('public.search_document_index') IS NOT NULL THEN
        ALTER TABLE public.search_document_index RENAME TO search_documents;
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.idx_search_documents_vector') IS NULL
       AND to_regclass('public.idx_search_document_index_vector') IS NOT NULL THEN
        ALTER INDEX public.idx_search_document_index_vector RENAME TO idx_search_documents_vector;
    END IF;
    IF to_regclass('public.idx_search_documents_sha256') IS NULL
       AND to_regclass('public.idx_search_document_index_sha256') IS NOT NULL THEN
        ALTER INDEX public.idx_search_document_index_sha256 RENAME TO idx_search_documents_sha256;
    END IF;
END
$$;
