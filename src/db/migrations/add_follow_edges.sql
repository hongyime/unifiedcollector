-- 2026-07-04: per-account follow graph for MULTI-ACCOUNT collection.
-- social_users.contexts (['follow','follower']) can't express "X follows account A
-- but not account B" — but with several owned accounts per platform that distinction
-- is the whole point (and it's what cross-account routing reasons over: which of my
-- accounts can see a given target). follow_edges records each owned account's own
-- graph directionally; social_users still gets the union for the network panel.
CREATE TABLE IF NOT EXISTS follow_edges (
    platform      text NOT NULL,
    owner_account text NOT NULL,          -- which of MY accounts this edge belongs to
    target_uid    text NOT NULL,          -- the other user (platform_user_id or handle)
    direction     text NOT NULL,          -- 'following' (owner follows target) | 'follower' (target follows owner)
    target_username text,
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, owner_account, target_uid, direction)
);
CREATE INDEX IF NOT EXISTS idx_follow_edges_owner ON follow_edges(platform, owner_account, direction);
CREATE INDEX IF NOT EXISTS idx_follow_edges_target ON follow_edges(platform, target_uid);
