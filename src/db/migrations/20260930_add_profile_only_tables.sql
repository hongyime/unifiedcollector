-- 2026-09-30: lightweight per-platform profile tables (Do Next #3).
-- Populated by src/collectors/<platform>/ - queue-driven, not firehose.
-- Griffin's bookmarklet library maps 1:1 onto these; we automate the
-- 5 highest-payoff platforms (Snapchat, PayPal, Airbnb, Bluesky, Pinterest)
-- from his demonstrated case narratives.
--
-- Common columns: username / display_name / bio / avatar_url + platform-
-- specific extras. raw_payload holds the full response for re-parse.

CREATE TABLE IF NOT EXISTS snapchat_profiles (
    username        text        PRIMARY KEY,
    display_name    text        NULL,
    bitmoji_url     text        NULL,
    bitmoji_version integer     NULL,
    snap_score      integer     NULL,
    raw_payload     jsonb       NULL,
    probed_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paypal_profiles (
    username         text        PRIMARY KEY,
    display_name     text        NULL,
    avatar_url       text        NULL,
    paypalme_currency text       NULL,
    paypalme_business boolean    NULL,
    raw_payload      jsonb       NULL,
    probed_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS airbnb_profiles (
    username         text        PRIMARY KEY,
    display_name     text        NULL,
    avatar_url       text        NULL,
    host_since       date        NULL,
    superhost        boolean     NULL,
    location_city    text        NULL,
    properties_count integer     NULL,
    reviews_count    integer     NULL,
    raw_payload      jsonb       NULL,
    probed_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bluesky_profiles (
    username        text        PRIMARY KEY,   -- e.g. bsky.social handle
    did             text        NULL,           -- did:plc:... decentralised id
    display_name    text        NULL,
    bio             text        NULL,
    avatar_url      text        NULL,
    follower_count  integer     NULL,
    following_count integer     NULL,
    posts_count     integer     NULL,
    indexed_at      timestamptz NULL,
    raw_payload     jsonb       NULL,
    probed_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bluesky_profiles_did ON bluesky_profiles(did) WHERE did IS NOT NULL;

CREATE TABLE IF NOT EXISTS pinterest_profiles (
    username        text        PRIMARY KEY,
    display_name    text        NULL,
    bio             text        NULL,
    avatar_url      text        NULL,
    follower_count  integer     NULL,
    pin_count       integer     NULL,
    board_count     integer     NULL,
    raw_payload     jsonb       NULL,
    probed_at       timestamptz NOT NULL DEFAULT now()
);
