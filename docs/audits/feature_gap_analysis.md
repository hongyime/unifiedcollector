# Feature Gap Analysis — New Unified Collector vs Archived Toolkits

> Source: Subagent 2 (Sonnet) feature-gap audit, 2026-06-13. `archive/` is reference-only.
> Companion to `collector_audit.md`. This is the PORT BACKLOG.

## Top 10 omitted features to port (prioritized)

1. **Strava GPS privacy-zone / truncation handling** — `_is_truncated`/`_to_lonlat`/`privacy_zone_start/end`/`stream_status='truncated_empty'` from `archive/stravatoolkit/ingestion/transform.py`. New `_collect_gps_streams`/`_upsert_activity` (`src/collectors/strava/__init__.py` ~604-800) store raw streams but have NO truncation-aware fallback. **LIKELY ROOT CAUSE of the `start_latlng` NULL bug** debugged earlier: privacy-zone activities return null summary start/end, and the new code doesn't derive start/end from the first/last non-null point of `streams.latlng`. **Highest priority.**
2. **Telegram `classify_document_media`** — reject animated stickers (.tgs/.webm), audio/voice notes; normalize video/image extensions. Old `archive/telegramtoolkit/src/core/media_policy.py` → new `_handle_document` (`telegram/__init__.py` ~1623) does naive `mime.split("/")[-1]`. High impact, low effort (wasted storage + broken extensions like `x-matroska`).
3. **Strava `/explore/*` page scraping** for cold-start athlete discovery (old `explore_scraper.py` Phase 1). New collector has no `/explore` discovery → add `_scrape_explore_pages()`.
4. **Strava segment-leaderboard athlete discovery** (old `explore_scraper.py` Phase 2). New `collect_segments_starred` only pulls own starred segments, not leaderboards.
5. **Generic cross-platform link-extractor + reconciler modules** — link extraction is now generic for source occurrences via `src/core/discovered_links.py` + `discovered_links`; WhatsApp still keeps its rich `wa_discovered_links` view. The generic reconciler part remains separate.
6. **YouTube description-link scraping** — shipped for `_upsert_video`; descriptions are extracted with `extract_all_links()` and persisted to `discovered_links`.
7. **Telegram multi-platform link extraction** — shipped for backfill and realtime message writes; message text/captions are extracted with `extract_all_links()` and persisted to `discovered_links`.
8. **Instagram follower/following profile filters** (`FILTER_MAX/MIN_FOLLOWERS`, `MAX/MIN_FOLLOWING`, `PUBLIC_ONLY`) with `filter_reason` persistence. New `_IGFetcher.fetch_edges` (~2795-2912) only caps per-edge counts. _(Note: `FILTER_MAX_FOLLOWERS=960` IS applied at profile-collection time in `_collect_user`; this gap is about edge/spider-time filtering + filter_reason audit.)_
9. **Two-stage avatar change detection** (URL-compare then `imagehash.phash()`) to avoid re-download on CDN URL churn — Strava/TikTok lack it; GitHub has `track_avatar_changes`/`reconcile_avatars` (promote to shared `src/core/profile_photo_tracker.py`).
10. **TikTok FAMOUS-FILTER on first-encounter** — skip the following-list fetch entirely for mega-accounts (old `spider.py:184` `over_threshold`), not just re-collection. Target `tiktok/__init__.py` `_collect_user` before `collect_following`.

## Per-platform "no significant gaps" (new ≥ old)
- **TikTok download chain**: new gallery-dl→yt-dlp→Playwright→API with partial-ingest-on-timeout is STRICTLY more robust than old. (So the planned "port Mode-β to TikTok" is unnecessary — already has browser fallback.)
- **YouTube**: new adds transcripts/comments/subscriptions/liked/playlists — superset of old.
- **Website**: `crawl_website`/`crawl_page`/`crawl_website_urls` near-direct port.
- **GitHub**: contributor/repo spider + avatar tracking ported.
- **Search**: multi-provider (ddg/bing/serper) + quota + paste-site expansion — comprehensive port.
- **WhatsApp/Beeper**: realtime consume + backfill + media archival + bridge sync — comprehensive.

## Lower-confidence / follow-up
- YouTube `resume_interrupted_downloads` (stuck-in-'downloading' recovery sweep) — partial/unverified.
- Website sitemap_parser / bulk importer / tor per-request rotation — unverified.
- Instagram account-rotation + resumable progress for following-media backfills — omitted.
- Telegram cross-account FloodWait fallback-before-sleep — partial (may be handled by unified rate limiter; verify).
