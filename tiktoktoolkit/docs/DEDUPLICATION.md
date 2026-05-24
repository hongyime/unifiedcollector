# Deduplication Strategy

## Overview

The toolkit uses a 3-layer deduplication system to prevent wasting bandwidth on duplicate downloads.

## How It Works

### Layer 1: Tracker Pre-Check (BEFORE download)
- Fetches video IDs from TikTok (metadata only, ~1KB per video)
- Checks SQLite database: `is_downloaded_in_folder(username, video_id, target_dir)`
- Adjusts download limit to only NEW videos
- **If all videos tracked → SKIPS DOWNLOAD ENTIRELY**

**Example:**
```
User has 100 videos
You request limit=30
Tracker finds 25 already downloaded
→ Only attempts to download 5 NEW videos
```

### Layer 2: Gallery-dl File Check
- Gallery-dl checks if output file already exists on disk
- If exists → SKIPS DOWNLOAD (no bandwidth used)
- Config: `"skip": true` in `configs/gallery-dl.json`

### Layer 3: Post-Download Recording
- AFTER successful download, records video ID in SQLite database
- Enables Layer 1 pre-check for future runs
- Tracks even if files are moved/deleted

## Bandwidth Efficiency

**Scenario:** User with 100 videos, 80 already downloaded

### Without Deduplication:
```
Downloads: 100 videos (100 × 50MB = 5GB bandwidth)
Discards: 80 duplicates after hash check
Wasted bandwidth: 4GB (80%)
```

### With This System:
```
Layer 1 Pre-Check: Identifies 20 new videos
Layer 2 File Check: 18 exist on disk, 2 missing
Downloads: 2 videos (2 × 50MB = 100MB bandwidth)
Wasted bandwidth: 0MB (0%)
```

**Efficiency gain: 50x less bandwidth!**

## Tracker Database

### Location
- SQLite: `configs/download_tracker.sqlite`
- JSON backup: `configs/download_tracker.json.backup`

### Benefits

**1. Survives File Moves**
```
Video downloaded to: downloads/username_alice/video123.mp4
User moves file to: archive/2024/video123.mp4
Next run: Tracker still knows video123 is downloaded → SKIPS
```

**2. Survives File Deletion**
```
Video downloaded and tracked
User deletes file (by mistake or intentionally)
Next run: Tracker knows it was downloaded → SKIPS
```

**3. Per-Folder Tracking**
```python
is_downloaded_in_folder(username, vid, target_dir)
```

Allows downloading same video to different folders:
```
downloads/personal/video123.mp4  ✅ Downloaded
downloads/backup/video123.mp4    ✅ Can download again (different folder)
```

## Verification

### Check what's tracked:
```bash
python scripts/diagnostics/verify_dedup.py
```

### Check tracker for specific user:
```bash
sqlite3 configs/download_tracker.sqlite "SELECT video_id, filepath, size FROM videos WHERE username='username';"
```

### Import existing downloads:
```bash
python main.py utils import-existing --root downloads
```

## Configuration

### Current Settings (configs/providers.yaml):
```yaml
skip_existing: true           # Gallery-dl file-level skip
tracker_required: true        # Tracker pre-check enabled
tracker_db: configs/download_tracker.sqlite
tracker_hash: false          # Hash computation disabled (not needed for dedup)
```

## Hash Computation (Optional)

**Purpose:** Integrity verification, NOT deduplication  
**Default:** Disabled (`tracker_hash: false`)  
**When enabled:** Hashes computed AFTER download completes  
**Never:** Downloads a file just to hash it

## Summary

✅ **NO wasted bandwidth** - Downloads prevented BEFORE they happen  
✅ **3-layer deduplication** - Tracker → File check → Recording  
✅ **Idempotent** - Running same command multiple times downloads nothing  
✅ **Efficient** - Only metadata fetched for pre-check (~1KB per video)  
✅ **Smart** - Tracks videos even if files are moved/deleted  
✅ **Per-folder aware** - Can download same video to different locations
