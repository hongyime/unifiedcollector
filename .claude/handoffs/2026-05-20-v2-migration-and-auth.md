# Handoff: UnifiedCollector V2 Migration & Production Setup

## Current State Summary
We have just completed a massive **V2 Architecture Redesign**. The system is now transitioned from a simple media downloader to a comprehensive data archiver. All 10 platforms (Instagram, Telegram, WhatsApp, TikTok, YouTube, GitHub, Strava, Lemon8, Website, Search) have been upgraded to support detailed data collection (captions, comments, transcripts, GPS, etc.) and autonomous spidering.

The system is currently in **Production Mode** (`SLIDING_WINDOW_ENABLED=true`), with Docker containers running. However, we are in the middle of a **one-by-one login verification phase** because several accounts require interactive authentication (2FA) or fresh browser cookies.

## Important Context
- **Database:** PostgreSQL (v16) with `pgvector`. All tables are converted to `TIMESTAMPTZ` to handle Python timezone-aware datetimes.
- **Storage:** All data is saved to `Z:/unifiedcollector/`. Media and Metadata JSON files are stored in per-platform, per-account subdirectories (e.g., `Z:/unifiedcollector/media/instagram/account_1/`).
- **Dashboard:** Running at [http://localhost:8700](http://localhost:8700). Login authentication has been **bypassed** for direct access.
- **Timezone Logic:** Use `datetime.now(timezone.utc)` for all database timestamps. Naive timestamps will cause crashes.

## Immediate Next Steps (The "One-by-One" Login Plan)
1. **Instagram Account 1 (bryanseah234):** 
   - User has re-exported cookies to `credentials/instagram/cookies/bryanseah234.txt`.
   - **Action:** Verify if these cookies work using a direct local script. If not, trigger a manual 2FA login where the user can enter the code in their terminal. **Do not attempt interactive 2FA inside the Docker container, as it causes EOF crashes.**
2. **Remaining Instagram Accounts (2-6):** Verify one by one.
3. **WhatsApp:** User has 2 accounts. QR codes were scanned locally, but need to verify if the session persistence is working inside the `unifiedcollector_collector` container.
4. **Telegram:** 4 sessions were migrated. Need to verify they are still authorized inside Docker.

## Decisions Made
- **Schema:** Separate tables per platform for clean, specific data modeling.
- **Safety Net:** Every media download automatically triggers a `_metadata.json` and `_raw.json` save to the Z: drive to ensure no data loss even if DB schema lags.
- **Auth:** Bypassed dashboard login to simplify local operations for the user.
- **Isolation:** Each scraping account gets its own dedicated folder on the Z: drive to prevent cross-contamination.

## Critical Files
- `.env`: Master configuration (secrets, rate limits, drive paths).
- `src/db/schemas/`: V2 SQL definitions for all platforms.
- `src/core/base_collector.py`: Logic for atomic file saving and metadata safety nets.
- `src/collectors/`: The 10 specialized scrapers.
- `docker/docker-compose.yml`: Stack definition with new volume mounts for credentials.

## Pending Work / Known Issues
- **Instagram 429:** High rate of 429 errors during testing; may need longer cooldowns or better proxy rotation.
- **Telegram EOF:** Collector sometimes crashes with `EOF when reading a line` when attempting interactive login inside Docker. **Recommendation:** Always run interactive logins via local host python first to generate the `.session` file before mounting to Docker.
- **Disk Usage:** Machine is filling up. Identified old `.db` files in `githubtoolkit/data/` and `youtubetoolkit/data/` as candidates for deletion after confirming migration success.
