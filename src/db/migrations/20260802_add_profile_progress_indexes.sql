-- Keep dashboard source-matrix profile progress cheap under live collection load.
SET lock_timeout = '2s';

DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT *
        FROM (VALUES
            ('instagram_profiles', 'updated_at', 'idx_instagram_profiles_updated_at'),
            ('tiktok_profiles', 'updated_at', 'idx_tiktok_profiles_updated_at'),
            ('lemon8_profiles', 'updated_at', 'idx_lemon8_profiles_updated_at'),
            ('facebook_profiles', 'updated_at', 'idx_facebook_profiles_updated_at'),
            ('x_profiles', 'updated_at', 'idx_x_profiles_updated_at'),
            ('youtube_channels', 'updated_at', 'idx_youtube_channels_updated_at'),
            ('github_users', 'collected_at', 'idx_github_users_collected_at'),
            ('strava_athletes', 'updated_at', 'idx_strava_athletes_updated_at')
        ) AS v(table_name, column_name, index_name)
    LOOP
        IF to_regclass(item.table_name) IS NOT NULL THEN
            EXECUTE format(
                'CREATE INDEX IF NOT EXISTS %I ON %I (%I)',
                item.index_name,
                item.table_name,
                item.column_name
            );
        END IF;
    END LOOP;
END $$;
