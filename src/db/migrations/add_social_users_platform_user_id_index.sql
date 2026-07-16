-- Complements idx_social_users_username for analyzer /entities/{id}/associates:
-- tagged-media owners resolve through social_users.platform_user_id.
SET lock_timeout = '2s';

CREATE INDEX IF NOT EXISTS idx_social_users_platform_user_id
ON social_users (platform, platform_user_id)
WHERE platform_user_id IS NOT NULL;

RESET lock_timeout;
