-- Social graph edges for the dashboard /graph endpoint.
-- Cross-platform follower/following/mention/reply relationships.

CREATE TABLE IF NOT EXISTS graph_edges (
    id              SERIAL PRIMARY KEY,
    source          TEXT NOT NULL,          -- platform: github, telegram, etc.
    source_user     TEXT NOT NULL,          -- entity_id of the origin user
    target_user     TEXT NOT NULL,          -- entity_id of the target user
    edge_type       TEXT NOT NULL,          -- follows, mentions, replies_to, collaborator
    weight          INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, source_user, target_user, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges (source);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_user ON graph_edges (source_user);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_user ON graph_edges (target_user);
