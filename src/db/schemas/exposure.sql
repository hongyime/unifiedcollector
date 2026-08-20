CREATE TABLE IF NOT EXISTS exposure_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_scope TEXT,
    query TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    title TEXT,
    snippet TEXT,
    detected_secret BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (url, query)
);

CREATE INDEX IF NOT EXISTS idx_exposure_findings_collected
    ON exposure_findings (collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_exposure_findings_category
    ON exposure_findings (category, severity);
CREATE INDEX IF NOT EXISTS idx_exposure_findings_domain
    ON exposure_findings (domain);
