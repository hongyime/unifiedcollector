# Refactoring Plan: From CSV+JSON to DB-First

## Current State

```
User Analysis
    ↓
    └→ SQLite DB (users table)
    └→ Users.csv (exported)

Profile Photo Download
    ↓
    ├→ Reads Users.csv (input)
    ├→ Tracks in JSON (output)
    └→ Saves hashes to DB
    
Result: Multiple sources of truth, weak backup
```

## Target State

```
User Analysis
    ↓
    └→ SQLite DB
       ├→ users table (growing)
       ├→ Can export to CSV for archive
       
Profile Photo Download
    ↓
    └→ Reads from DB (same source)
    └→ Updates DB directly (atomic, durable)
    
Result: Single source of truth, atomic durability
```

## Migration Steps

### Phase 1: Add tracking columns to users table (NO CODE CHANGES YET)

```bash
# Run this once:
python

from toolkit.core.state_manager import get_state_manager
state = get_state_manager()

# Add columns
state.conn.execute('''
    ALTER TABLE users ADD COLUMN profile_photo_downloaded INTEGER DEFAULT 0
''')
state.conn.execute('''
    ALTER TABLE users ADD COLUMN profile_photo_last_checked TIMESTAMP
''')
state.conn.execute('''
    ALTER TABLE users ADD COLUMN profile_photo_count INTEGER DEFAULT 0
''')
state.conn.commit()
print("✅ Schema updated")
```

### Phase 2: Migrate existing JSON tracking to DB

```bash
python

import json
from toolkit.core.state_manager import get_state_manager

state = get_state_manager()

# Load old JSON tracking
with open("data/downloaded_profile_photos.json") as f:
    downloaded_set = json.load(f)  # e.g., ["123_photo_456", "789_photo_012"]

# For simplicity, mark all tracked users as downloaded
tracked_users = set()
for item in downloaded_set:
    user_id = item.split('_')[0]
    tracked_users.add(int(user_id))

# Update DB
for user_id in tracked_users:
    state.conn.execute('''
        UPDATE users 
        SET profile_photo_downloaded = 1, profile_photo_last_checked = CURRENT_TIMESTAMP
        WHERE user_id = ?
    ''', (user_id,))

state.conn.commit()
print(f"✅ Migrated {len(tracked_users)} users to DB")

exit()
```

### Phase 3: Update ProfilePhotoDownloader code

Replace the `__init__` method:

**OLD:**
```python
def __init__(self, csv_file_path, save_path, parallel_processor=None):
    self.csv_file_path = csv_file_path  # ← Remove this
    self.save_path = save_path
    ...
    self.downloaded_photos = set()  # ← Remove this
    self.load_downloaded_photos()   # ← Remove this
```

**NEW:**
```python
def __init__(self, save_path, parallel_processor=None):
    self.save_path = save_path
    self.parallel_processor = parallel_processor
    self.state = get_state_manager()
    
    Path(self.save_path).mkdir(parents=True, exist_ok=True)
    self._ensure_profile_tracking_columns()
```

Replace the download loop:

**OLD:**
```python
with open(self.csv_file_path) as f:
    for row in csv.DictReader(f):
        user_id = int(row['user_id'])
        # ... process ...
        
        if success:
            self.downloaded_photos.add(photo_id)
            self.save_downloaded_photos()  # Manual save
```

**NEW:**
```python
for user_row in self.load_users_from_db(filter_already_downloaded=True):
    user_id = user_row['user_id']
    # ... process ...
    
    if success:
        # Atomic, durable update
        self.on_profile_photo_download_success(user_id, photo_count)
```

Replace the `save_downloaded_photos()` method:

**OLD:**
```python
def save_downloaded_photos(self):
    atomic_json_write(self.profile_photos_file, list(self.downloaded_photos))
```

**NEW:**
```python
def on_profile_photo_download_success(self, user_id: int, photo_count: int):
    self.state.conn.execute('''
        UPDATE users 
        SET 
            profile_photo_downloaded = 1,
            profile_photo_last_checked = CURRENT_TIMESTAMP,
            profile_photo_count = ?
        WHERE user_id = ?
    ''', (photo_count, user_id))
    self.state.conn.commit()
```

### Phase 4: Update main.py menu

Replace the menu call:

**OLD:**
```python
downloader = ProfilePhotoDownloader(
    csv_file_path=csv_file,
    save_path=download_path,
    parallel_processor=self.parallel_processor
)
```

**NEW:**
```python
downloader = ProfilePhotoDownloader(
    save_path=download_path,
    parallel_processor=self.parallel_processor
)
```

### Phase 5: Remove CSV dependency (optional but clean)

The "Analyze Users" feature can stop exporting to CSV, or make it optional:

**In main.py feature registry:**
```python
# Option 1: Remove CSV export entirely
def on_analyze_users_complete():
    # Just keep data in DB
    pass

# Option 2: Make export optional (for portability only)
if user_confirms("Export as CSV for portability? (optional)"):
    state.export_users_to_csv("data/Users.csv")
```

### Phase 6: Cleanup

Remove these files (they're no longer needed):
- `data/Users.csv` - regenerate on demand if needed
- `data/downloaded_profile_photos.json` - now in DB
- Code that loads/saves JSON tracking

---

## Validation Checklist

After each phase:

```bash
# Phase 1 validation
# ✅ Check DB schema has new columns
python -c "
from toolkit.core.state_manager import get_state_manager
s = get_state_manager()
cursor = s.conn.execute('PRAGMA table_info(users)')
cols = [row[1] for row in cursor]
assert 'profile_photo_downloaded' in cols
print('✅ Schema OK')
"

# Phase 2 validation
# ✅ Check migration count
python -c "
from toolkit.core.state_manager import get_state_manager
s = get_state_manager()
cursor = s.conn.execute('SELECT COUNT(*) FROM users WHERE profile_photo_downloaded=1')
count = cursor.fetchone()[0]
print(f'✅ {count} users marked as downloaded')
"

# Phase 3 validation
# ✅ Run ProfilePhotoDownloader with updated code
# Should NOT error on missing CSV
# Should update DB on successful download

# Phase 4 validation
# ✅ Menu should work without passing csv_file_path

# Phase 6 validation
# ✅ CSV should NOT be required to run profile downloader
python -c "
import os
from toolkit.managers.download_profile_photos import ProfilePhotoDownloader
os.remove('data/Users.csv')  # Delete CSV
downloader = ProfilePhotoDownloader(save_path='downloads/profiles')
print('✅ ProfilePhotoDownloader initializes without CSV')
"
```

---

## Files to Modify

1. ✅ `toolkit/core/state_manager.py` - Ensure DB schema includes tracking columns (already there)
2. 🔧 `toolkit/managers/download_profile_photos.py` - Refactor to use DB instead of CSV+JSON
3. 🔧 `main.py` - Update menu calls to ProfilePhotoDownloader
4. 📋 `tests/test_profile_photo_downloader.py` - Update tests (if exists)

---

## Benefits After Migration

| Before | After |
|--------|-------|
| ❌ CSV can get out of sync | ✅ Single source (DB) |
| ❌ JSON tracking is lossy | ✅ Atomic DB updates |
| ❌ No resume capability | ✅ Query DB for resume |
| ❌ No durability guarantees | ✅ WAL = durability |
| ❌ Can't query "which users have photos" | ✅ Simple SQL query |
| ❌ Slow (load full CSV, parse JSON) | ✅ Fast (streaming DB queries) |
| ❌ Ctrl+C loses progress | ✅ Ctrl+C preserves committed data |

---

## Estimated Effort

- Phase 1: 2 minutes (SQL commands)
- Phase 2: 5 minutes (migration script)
- Phase 3: 30 minutes (code refactoring)
- Phase 4: 5 minutes (menu updates)
- Phase 5: 10 minutes (cleanup)
- Phase 6: 5 minutes (remove files)

**Total: ~1 hour for complete migration**

