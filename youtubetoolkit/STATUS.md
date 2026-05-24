# YouTube Toolkit — Status

## Status: PRODUCTION-READY (2026-05-13)
Tests: 24/24 passing

## What it does
Scrape → queue → download workflow for YouTube content.
- OAuth scraping: liked videos, subscriptions (YouTube Data API v3)
- API-free scraping: target channels, custom URLs/playlists (yt-dlp)
- SQLite queue with JSON backup/restore
- Browser cookie auth (Chrome, Edge, Firefox)
- Interactive Windows launcher (15 menu options)
- Rate-limited downloads with exponential backoff on 429 errors
- Channel-based folder organization for downloaded files

## Entry points
| File | Purpose |
|------|---------|
| `start_toolkit.bat` | Interactive launcher (recommended) |
| `main.py` | Python entry point |
| `scripts/batch_downloader.py` | CLI batch download |
| `scripts/scrape_*.py` | Individual scrapers |

## Architecture
```
src/                      # Core library
  config.py               # .env + config.json loader
  data_manager_streamlined.py  # SQLite layer
  video_processor.py      # yt-dlp download engine
  rate_limiter.py         # Human-like rate limiting
  download_structurer.py  # Channel-based file organization
  app_paths.py            # Centralized path constants
  auth_cache.py           # OAuth credential cache
  resilience.py           # Shutdown event + retry utils

scripts/                  # Entry points
  batch_downloader.py
  scrape_liked_videos_enhanced.py
  subscription_processor.py
  scrape_targets.py
  scrape_custom_playlist.py
  logout_account.py
  validate_installation.py

data/                     # Runtime (gitignored)
  youtube_data.db         # SQLite queue
  client_secret.json      # User-provided OAuth credentials
  target_channels.txt     # Channels to scrape
```

## Bugs fixed (2026-05-16)
1. `src/video_processor.py` — Enabled `remote_components: ['ejs:github']` in `yt-dlp` options to fix "n" challenge solving failures and "Requested format is not available" errors.
2. `requirements.txt` — Updated `yt-dlp` to `2025.2.19` or later.

## Bugs fixed (2026-05-13)
1. `src/video_processor.py` — `RATE_LIMITING_AVAILABLE` NameError (was `RATE_LIMITER_AVAILABLE` at module level)
2. `src/video_processor.py` — `download_with_rate_limiting` passed unsupported kwargs to `download_youtube_video`
3. `scripts/batch_downloader.py` — duration filter silently dropped videos with unknown duration (None)
4. `tests/test_download_flows.py` — tests needed to patch `download_with_rate_limiting=None` to reach mocked `download_youtube_video`

## Cleaned up
- `add_rate_limiting_function.py` (root patch artifact)
- `enhance_video_processor.py` (root patch artifact)
- `fix_batch_downloader.py` (root patch artifact)
- `IMPLEMENTATION_SUMMARY.md` (developer notes)
- `common/` directory (stale .pyc, no source)

## Requires to run
- Python 3.10+, ffmpeg
- Run `setup.bat` once to create `.venv`
- For OAuth scrapers: place `client_secret.json` in `data/`
