
-- V2 Schema Adjustment for Existing Tables

-- Helper: Function to check if a table is empty
CREATE OR REPLACE FUNCTION is_table_empty(tbl_name text) RETURNS boolean AS '
DECLARE
  row_count integer;
BEGIN
  EXECUTE format(''SELECT count(*) FROM %I'', tbl_name) INTO row_count;
  RETURN row_count = 0;
END;
' LANGUAGE plpgsql;

-- INSTAGRAM
DO '
BEGIN
    IF is_table_empty(''instagram_profiles'') THEN
        DROP TABLE IF EXISTS instagram_comments CASCADE;
        DROP TABLE IF EXISTS instagram_posts CASCADE;
        DROP TABLE IF EXISTS instagram_profiles CASCADE;
        
        CREATE TABLE instagram_profiles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform_user_id VARCHAR(255) UNIQUE NOT NULL,
            username VARCHAR(255),
            full_name TEXT,
            bio TEXT,
            followers_count INTEGER DEFAULT 0,
            following_count INTEGER DEFAULT 0,
            posts_count INTEGER DEFAULT 0,
            is_verified BOOLEAN DEFAULT FALSE,
            is_private BOOLEAN DEFAULT FALSE,
            profile_pic_url TEXT,
            email TEXT,
            phone TEXT,
            external_url TEXT,
            collected_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
    END IF;
END ';

CREATE TABLE IF NOT EXISTS instagram_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_post_id VARCHAR(255) UNIQUE NOT NULL,
    profile_id UUID REFERENCES instagram_profiles(id) ON DELETE SET NULL,
    media_type VARCHAR(50),
    caption TEXT,
    hashtags TEXT[],
    mentions TEXT[],
    location_name TEXT,
    location_lat FLOAT,
    location_lng FLOAT,
    likes_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    shares_count INTEGER DEFAULT 0,
    saves_count INTEGER DEFAULT 0,
    reach_count INTEGER,
    impressions_count INTEGER,
    video_duration INTEGER,
    music_title TEXT,
    music_author TEXT,
    is_ad BOOLEAN DEFAULT FALSE,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB
);

CREATE TABLE IF NOT EXISTS instagram_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_comment_id VARCHAR(255) UNIQUE NOT NULL,
    post_id UUID REFERENCES instagram_posts(id) ON DELETE CASCADE,
    author_username VARCHAR(255),
    author_platform_id VARCHAR(255),
    author_followers_count INTEGER,
    text TEXT,
    like_count INTEGER DEFAULT 0,
    parent_comment_id VARCHAR(255),
    is_reply BOOLEAN DEFAULT FALSE,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW()
);

-- TELEGRAM
DO '
BEGIN
    IF is_table_empty(''telegram_chats'') THEN
        DROP TABLE IF EXISTS telegram_messages CASCADE;
        DROP TABLE IF EXISTS telegram_chats CASCADE;
        
        CREATE TABLE telegram_chats (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform_chat_id VARCHAR(255) UNIQUE NOT NULL,
            title VARCHAR(500),
            username VARCHAR(255),
            type VARCHAR(20),
            description TEXT,
            members_count INTEGER DEFAULT 0,
            collected_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
    END IF;
END ';

CREATE TABLE IF NOT EXISTS telegram_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_message_id VARCHAR(255) UNIQUE NOT NULL,
    chat_id UUID REFERENCES telegram_chats(id) ON DELETE CASCADE,
    sender_id UUID REFERENCES telegram_users(id) ON DELETE SET NULL,
    text TEXT,
    caption TEXT,
    media_type VARCHAR(50),
    media_file_id VARCHAR(255),
    reply_to_message_id VARCHAR(255),
    is_edited BOOLEAN DEFAULT FALSE,
    edit_date TIMESTAMP,
    forward_from_chat_id VARCHAR(255),
    forward_from_message_id VARCHAR(255),
    via_bot_id VARCHAR(255),
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB
);

-- WHATSAPP
DO '
BEGIN
    IF is_table_empty(''whatsapp_chats'') THEN
        DROP TABLE IF EXISTS whatsapp_messages CASCADE;
        DROP TABLE IF EXISTS whatsapp_chats CASCADE;
        
        CREATE TABLE whatsapp_chats (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform_chat_id VARCHAR(255) UNIQUE NOT NULL,
            name VARCHAR(500),
            is_group BOOLEAN DEFAULT FALSE,
            participant_count INTEGER DEFAULT 0,
            description TEXT,
            collected_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
    END IF;
END ';

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_message_id VARCHAR(255) UNIQUE NOT NULL,
    chat_id UUID REFERENCES whatsapp_chats(id) ON DELETE CASCADE,
    sender_id UUID REFERENCES whatsapp_users(id) ON DELETE SET NULL,
    from_me BOOLEAN DEFAULT FALSE,
    text TEXT,
    media_url TEXT,
    media_mime_type VARCHAR(100),
    media_size INTEGER,
    thumbnail_url TEXT,
    quoted_message_id VARCHAR(255),
    quoted_text TEXT,
    forward_from_name VARCHAR(255),
    timestamp TIMESTAMP,
    status VARCHAR(50),
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB
);

-- TIKTOK
DO '
BEGIN
    IF is_table_empty(''tiktok_profiles'') THEN
        DROP TABLE IF EXISTS tiktok_comments CASCADE;
        DROP TABLE IF EXISTS tiktok_posts CASCADE;
        DROP TABLE IF EXISTS tiktok_profiles CASCADE;
        
        CREATE TABLE tiktok_profiles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform_user_id VARCHAR(255) UNIQUE NOT NULL,
            username VARCHAR(255),
            nickname VARCHAR(255),
            avatar_url TEXT,
            bio TEXT,
            following_count INTEGER DEFAULT 0,
            followers_count INTEGER DEFAULT 0,
            heart_count INTEGER DEFAULT 0,
            video_count INTEGER DEFAULT 0,
            digg_count INTEGER DEFAULT 0,
            is_verified BOOLEAN DEFAULT FALSE,
            is_private BOOLEAN DEFAULT FALSE,
            collected_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
    END IF;
END ';

CREATE TABLE IF NOT EXISTS tiktok_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_post_id VARCHAR(255) UNIQUE NOT NULL,
    profile_id UUID REFERENCES tiktok_profiles(id) ON DELETE SET NULL,
    video_url TEXT,
    cover_image_url TEXT,
    title TEXT,
    description TEXT,
    hashtags TEXT[],
    mentions TEXT[],
    challenges TEXT[],
    music_id VARCHAR(255),
    music_title TEXT,
    music_author TEXT,
    music_duration INTEGER,
    effect_ids TEXT[],
    stickers TEXT[],
    duet_enabled BOOLEAN DEFAULT FALSE,
    stitch_enabled BOOLEAN DEFAULT FALSE,
    view_count INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    duration INTEGER,
    create_time TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB
);

CREATE TABLE IF NOT EXISTS tiktok_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_comment_id VARCHAR(255) UNIQUE NOT NULL,
    post_id UUID REFERENCES tiktok_posts(id) ON DELETE CASCADE,
    author_username VARCHAR(255),
    author_nickname VARCHAR(255),
    author_avatar_url TEXT,
    text TEXT,
    like_count INTEGER DEFAULT 0,
    reply_count INTEGER DEFAULT 0,
    parent_comment_id VARCHAR(255),
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW()
);

-- YOUTUBE
DO '
BEGIN
    IF is_table_empty(''youtube_channels'') THEN
        DROP TABLE IF EXISTS youtube_comments CASCADE;
        DROP TABLE IF EXISTS youtube_transcripts CASCADE;
        DROP TABLE IF EXISTS youtube_videos CASCADE;
        DROP TABLE IF EXISTS youtube_channels CASCADE;
        
        CREATE TABLE youtube_channels (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform_channel_id VARCHAR(255) UNIQUE NOT NULL,
            title VARCHAR(500),
            description TEXT,
            custom_url VARCHAR(255),
            published_at TIMESTAMP,
            thumbnail_url TEXT,
            view_count BIGINT DEFAULT 0,
            subscriber_count BIGINT DEFAULT 0,
            video_count INTEGER DEFAULT 0,
            hidden_subscriber_count BOOLEAN DEFAULT FALSE,
            country VARCHAR(100),
            keywords TEXT[],
            collected_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
    END IF;
END ';

CREATE TABLE IF NOT EXISTS youtube_videos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_video_id VARCHAR(255) UNIQUE NOT NULL,
    channel_id UUID REFERENCES youtube_channels(id) ON DELETE SET NULL,
    title TEXT,
    description TEXT,
    tags TEXT[],
    category_id VARCHAR(50),
    duration VARCHAR(50),
    dimension VARCHAR(10),
    definition VARCHAR(10),
    caption VARCHAR(50),
    licensed_content BOOLEAN DEFAULT FALSE,
    view_count BIGINT DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    dislike_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    favorite_count INTEGER DEFAULT 0,
    platform_published_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB
);

CREATE TABLE IF NOT EXISTS youtube_transcripts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id UUID REFERENCES youtube_videos(id) ON DELETE CASCADE,
    language VARCHAR(10),
    is_generated BOOLEAN DEFAULT FALSE,
    content TEXT,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS youtube_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_comment_id VARCHAR(255) UNIQUE NOT NULL,
    video_id UUID REFERENCES youtube_videos(id) ON DELETE CASCADE,
    author_name VARCHAR(255),
    author_channel_id VARCHAR(255),
    author_thumbnail_url TEXT,
    text_original TEXT,
    like_count INTEGER DEFAULT 0,
    parent_comment_id VARCHAR(255),
    is_reply BOOLEAN DEFAULT FALSE,
    platform_published_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW()
);

-- GITHUB
DO '
BEGIN
    IF is_table_empty(''github_repos'') THEN
        DROP TABLE IF EXISTS github_commits CASCADE;
        DROP TABLE IF EXISTS github_issues CASCADE;
        DROP TABLE IF EXISTS github_readmes CASCADE;
        DROP TABLE IF EXISTS github_repos CASCADE;
        
        CREATE TABLE github_repos (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform_repo_id BIGINT UNIQUE NOT NULL,
            name VARCHAR(255),
            full_name VARCHAR(500),
            description TEXT,
            homepage TEXT,
            language VARCHAR(100),
            stargazers_count INTEGER DEFAULT 0,
            watchers_count INTEGER DEFAULT 0,
            forks_count INTEGER DEFAULT 0,
            open_issues_count INTEGER DEFAULT 0,
            topics TEXT[],
            license VARCHAR(100),
            platform_created_at TIMESTAMP,
            platform_updated_at TIMESTAMP,
            collected_at TIMESTAMP DEFAULT NOW(),
            metadata JSONB
        );
    END IF;
END ';

CREATE TABLE IF NOT EXISTS github_readmes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    content TEXT,
    sha VARCHAR(50),
    size INTEGER,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_commits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    sha VARCHAR(40) UNIQUE NOT NULL,
    author_name VARCHAR(255),
    author_email VARCHAR(255),
    author_login VARCHAR(255),
    committer_name VARCHAR(255),
    committer_email VARCHAR(255),
    message TEXT,
    date TIMESTAMP,
    files_changed INTEGER,
    insertions INTEGER,
    deletions INTEGER,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    platform_issue_id INTEGER UNIQUE NOT NULL,
    number INTEGER,
    title TEXT,
    body TEXT,
    state VARCHAR(20),
    is_pull_request BOOLEAN DEFAULT FALSE,
    labels TEXT[],
    assignees TEXT[],
    milestone VARCHAR(255),
    comments_count INTEGER DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    closed_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW()
);

-- STRAVA
DO '
BEGIN
    IF is_table_empty(''strava_athletes'') THEN
        DROP TABLE IF EXISTS strava_activities CASCADE;
        DROP TABLE IF EXISTS strava_athletes CASCADE;
        
        CREATE TABLE strava_athletes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform_athlete_id BIGINT UNIQUE NOT NULL,
            username VARCHAR(255),
            firstname VARCHAR(255),
            lastname VARCHAR(255),
            profile TEXT,
            city VARCHAR(255),
            state VARCHAR(255),
            country VARCHAR(100),
            sex VARCHAR(1),
            weight FLOAT,
            height FLOAT,
            follower_count INTEGER DEFAULT 0,
            following_count INTEGER DEFAULT 0,
            created_at TIMESTAMP,
            collected_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
    END IF;
END ';

CREATE TABLE IF NOT EXISTS strava_activities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_activity_id BIGINT UNIQUE NOT NULL,
    athlete_id UUID REFERENCES strava_athletes(id) ON DELETE SET NULL,
    name VARCHAR(500),
    type VARCHAR(100),
    sport_type VARCHAR(100),
    workout_type INTEGER,
    description TEXT,
    distance FLOAT,
    moving_time INTEGER,
    elapsed_time INTEGER,
    total_elevation_gain FLOAT,
    average_speed FLOAT,
    max_speed FLOAT,
    average_heartrate INTEGER,
    max_heartrate INTEGER,
    average_cadence FLOAT,
    max_cadence FLOAT,
    average_temp FLOAT,
    weighted_average_watts INTEGER,
    max_watts INTEGER,
    kilojoules FLOAT,
    calories INTEGER,
    average_watts INTEGER,
    start_date TIMESTAMP,
    start_latlng VARCHAR(50),
    end_latlng VARCHAR(50),
    timezone VARCHAR(100),
    utc_offset INTEGER,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB
);

-- LEMON8
DO '
BEGIN
    IF is_table_empty(''lemon8_profiles'') THEN
        DROP TABLE IF EXISTS lemon8_posts CASCADE;
        DROP TABLE IF EXISTS lemon8_profiles CASCADE;
        
        CREATE TABLE lemon8_profiles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform_user_id VARCHAR(255) UNIQUE NOT NULL,
            username VARCHAR(255),
            nickname VARCHAR(255),
            avatar_url TEXT,
            bio TEXT,
            following_count INTEGER DEFAULT 0,
            followers_count INTEGER DEFAULT 0,
            like_count INTEGER DEFAULT 0,
            collected_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
    END IF;
END ';

CREATE TABLE IF NOT EXISTS lemon8_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_post_id VARCHAR(255) UNIQUE NOT NULL,
    profile_id UUID REFERENCES lemon8_profiles(id) ON DELETE SET NULL,
    title TEXT,
    description TEXT,
    image_urls TEXT[],
    video_url TEXT,
    music_title TEXT,
    hashtags TEXT[],
    mention_usernames TEXT[],
    location_name TEXT,
    like_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB
);

-- WEBSITE
CREATE TABLE IF NOT EXISTS website_pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id VARCHAR(500) REFERENCES website_targets(domain) ON DELETE CASCADE,
    url TEXT UNIQUE NOT NULL,
    url_hash VARCHAR(64),
    title TEXT,
    meta_description TEXT,
    meta_keywords TEXT[],
    h1_tags TEXT[],
    content_text TEXT,
    content_html TEXT,
    internal_links TEXT[],
    external_links TEXT[],
    images JSONB,
    structured_data JSONB,
    status_code INTEGER,
    fetched_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW()
);

-- SEARCH
CREATE TABLE IF NOT EXISTS search_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id INTEGER REFERENCES search_queries(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT,
    snippet TEXT,
    rank INTEGER,
    domain VARCHAR(255),
    date_published TEXT,
    collected_at TIMESTAMP DEFAULT NOW()
);

