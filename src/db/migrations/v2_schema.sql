-- Migration V2: Full Data Collection & Schema Redesign

-- 1. INSTAGRAM
CREATE TABLE IF NOT EXISTS instagram_profiles (
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

CREATE TABLE IF NOT EXISTS instagram_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    source VARCHAR(50),
    source_post_id VARCHAR(255),
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_attempt TIMESTAMP,
    error_message TEXT,
    collected_at TIMESTAMP DEFAULT NOW()
);

-- 2. TELEGRAM
CREATE TABLE IF NOT EXISTS telegram_chats (
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

CREATE TABLE IF NOT EXISTS telegram_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    phone VARCHAR(50),
    bio TEXT,
    photo_url TEXT,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

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

CREATE TABLE IF NOT EXISTS telegram_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_chat_id VARCHAR(255) UNIQUE NOT NULL,
    title VARCHAR(500),
    source VARCHAR(50),
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW()
);

-- 3. WHATSAPP
CREATE TABLE IF NOT EXISTS whatsapp_chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_chat_id VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(500),
    is_group BOOLEAN DEFAULT FALSE,
    participant_count INTEGER DEFAULT 0,
    description TEXT,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS whatsapp_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255),
    pushname VARCHAR(255),
    status TEXT,
    photo_url TEXT,
    about TEXT,
    collected_at TIMESTAMP DEFAULT NOW()
);

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

-- 4. TIKTOK
CREATE TABLE IF NOT EXISTS tiktok_profiles (
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

CREATE TABLE IF NOT EXISTS tiktok_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    source VARCHAR(50),
    source_post_id VARCHAR(255),
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW()
);

-- 5. YOUTUBE
CREATE TABLE IF NOT EXISTS youtube_channels (
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

CREATE TABLE IF NOT EXISTS youtube_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_channel_id VARCHAR(255) UNIQUE NOT NULL,
    source VARCHAR(50),
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW()
);

-- 6. GITHUB
CREATE TABLE IF NOT EXISTS github_repos (
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

CREATE TABLE IF NOT EXISTS github_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id BIGINT UNIQUE NOT NULL,
    login VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255),
    company VARCHAR(255),
    blog TEXT,
    location VARCHAR(255),
    email VARCHAR(255),
    bio TEXT,
    public_repos_count INTEGER DEFAULT 0,
    public_gists_count INTEGER DEFAULT 0,
    followers_count INTEGER DEFAULT 0,
    following_count INTEGER DEFAULT 0,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type VARCHAR(20),
    target_identifier VARCHAR(500) NOT NULL,
    source VARCHAR(50),
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW()
);

-- 7. STRAVA
CREATE TABLE IF NOT EXISTS strava_athletes (
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

CREATE TABLE IF NOT EXISTS strava_gps_streams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_id UUID REFERENCES strava_activities(id) ON DELETE CASCADE UNIQUE,
    latlng JSONB,
    altitude JSONB,
    distance JSONB,
    time JSONB,
    heartrate JSONB,
    cadence JSONB,
    watts JSONB,
    speed JSONB,
    grade_smooth JSONB,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS strava_segments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_segment_id BIGINT UNIQUE NOT NULL,
    name VARCHAR(500),
    activity_type VARCHAR(50),
    distance FLOAT,
    average_grade FLOAT,
    maximum_grade FLOAT,
    elevation_high FLOAT,
    elevation_low FLOAT,
    start_latlng VARCHAR(50),
    end_latlng VARCHAR(50),
    climb_category INTEGER,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS strava_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_athlete_id BIGINT UNIQUE NOT NULL,
    source VARCHAR(50),
    source_activity_id BIGINT,
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW()
);

-- 8. LEMON8
CREATE TABLE IF NOT EXISTS lemon8_profiles (
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

CREATE TABLE IF NOT EXISTS lemon8_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id VARCHAR(255) UNIQUE NOT NULL,
    source VARCHAR(50),
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW()
);

-- 9. WEBSITE
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
    updated_at TIMESTAMP DEFAULT NOW()
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

-- 10. SEARCH
CREATE TABLE IF NOT EXISTS search_queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query VARCHAR(500) NOT NULL,
    engine VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW()
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
