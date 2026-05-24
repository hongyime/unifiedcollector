-- Website and Search V2 Schema

CREATE TABLE IF NOT EXISTS website_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain VARCHAR(500) UNIQUE NOT NULL,
    name VARCHAR(255),
    start_url TEXT,
    robots_txt TEXT,
    sitemap_url TEXT,
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_target_domain_website UNIQUE (domain)
);

CREATE TABLE IF NOT EXISTS website_pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_id UUID REFERENCES website_targets(id) ON DELETE CASCADE,
    url TEXT UNIQUE NOT NULL,
    url_hash VARCHAR(64),
    title TEXT,
    meta_description TEXT,
    meta_keywords TEXT[],
    h1_tags TEXT[],
    content_text TEXT, -- Extracted clean text
    content_html TEXT, -- Full HTML
    internal_links TEXT[],
    external_links TEXT[],
    images JSONB, -- [{src, alt, width, height}]
    structured_data JSONB, -- JSON-LD, microdata
    status_code INTEGER,
    fetched_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_page_url_website UNIQUE (url)
);

CREATE TABLE IF NOT EXISTS search_queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query VARCHAR(500) NOT NULL,
    engine VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_search_query UNIQUE (query, engine)
);

CREATE TABLE IF NOT EXISTS search_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id UUID REFERENCES search_queries(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT,
    snippet TEXT,
    rank INTEGER,
    domain VARCHAR(255),
    date_published TEXT,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_website_pages_target ON website_pages(target_id);
CREATE INDEX IF NOT EXISTS idx_search_results_query ON search_results(query_id);
