-- 2026-07-06 (Option B follow-up to #39): media_url column for TikTok DMs.
--
-- TikTok DM content JSON differs by aweType:
--   aweType=0  -> text (already handled: content_json.text)
--   aweType=1  -> sticker (URL under .stickerUrl or nested .imageInfo)
--   aweType=2  -> image (URL under .imageUri / .display_image / .imageInfo)
--   aweType=3  -> video (URL under .videoInfo.playAddr / .videoInfo.playUri)
--   aweType=5  -> audio note (URL under .audioInfo.playUrl)
--   aweType=6  -> gif (URL under .giphyUrl or .gifUrl)
--   aweType=7  -> shared post (URL under .item.share.share_url or .awemeId)
--   aweType=8+ -> other (persistent as raw_content for future extraction)
--
-- Extraction is speculative — the exact field names for aweType>=1 are best-
-- effort per public reverse engineering (community TikTok IM SDKs on
-- GitHub). Real samples of each aweType will let us tighten the mapping.
-- Until then: raw_content stays as-is (JSONB captures everything for post-hoc
-- extraction), media_url gets populated where the heuristic finds a URL, and
-- unknown aweTypes silently fall through with media_url=NULL.

ALTER TABLE tiktok_dm ADD COLUMN IF NOT EXISTS media_url TEXT;
CREATE INDEX IF NOT EXISTS idx_tt_dm_awetype ON tiktok_dm(awe_type) WHERE awe_type IS NOT NULL AND awe_type != 0;
