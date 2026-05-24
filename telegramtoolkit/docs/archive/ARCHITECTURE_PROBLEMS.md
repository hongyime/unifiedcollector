# Architecture Analysis: Why Current Design Falls Short

## Problem 1: JSON Backups Are NOT Durable

**Current state:**
```python
# download_profile_photos.py, line 406-415
def save_downloaded_photos(self):
    # Comment admits it: "keep JSON as primary for profile photos"
    # No database backing, just JSON file
    atomic_json_write(self.profile_photos_file, list(self.downloaded_photos))
```

**Why this is weak:**
- Only writes to JSON when `save_downloaded_photos()` is called explicitly
- **If you interrupt before calling it → data lost** (unlike DB which chunks writes)
- JSON is only a "backup" in name, not in practice
- No recovery mechanism if JSON corrupts

**Result:** You're right - JSON is "filmy". It's a *false backup*.

---

## Problem 2: CSV Exists for Legacy Reasons

**Current flow:**
```python
# download_profile_photos.py, line 1351
csv_file = "data/Users.csv"  # Hardcoded

# Then reads it:
with open(csv_file, 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        # Process each user
```

**Why this is odd:**
- Database has all the same users
- CSV is slower (disk I/O, in-memory load) vs. DB query streaming
- If CSV gets out of sync, you're working with stale data
- The CSV export happens offline (after "Analyze Users" completes)

**Why it exists anyway:**
- Legacy from before DB migration was complete
- CSV is a "checkpoint" - you can inspect it in Excel
- Decouples input (users to download from) from output (photos going to disk)
- But technically unnecessary

---

## Problem 3: Downloaded Profile Data NOT Stored

**Current state:**
```python
# Only tracks THAT we downloaded, not what we got
self.downloaded_photos = set()  # {"user_123_photo_456", ...}

# Saved to JSON only:
atomic_json_write(self.profile_photos_file, list(self.downloaded_photos))
```

**What SHOULD happen:**
```python
# Should store in users table:
state.save_user(
    user_id=123,
    profile_photo_downloaded=True,      # NEW
    last_profile_check="2026-04-15",    # NEW
    profile_photo_path="/downloads/...", # NEW
)
```

**Why it matters:**
- You can't query: "Which users have profile photos?"
- You can't track: "When was this user's profile last checked?"
- If JSON corrupts, you have no DB recovery
- Each run has to reload JSON from disk

---

## Better Architecture: DB-First + WAL-Backed

Here's what SHOULD happen:

### 1. **Query users directly from DB** (not CSV)

```python
# Instead of:
with open("data/Users.csv") as f:
    users = csv.DictReader(f)

# Do this:
state = get_state_manager()
cursor = state.conn.execute("SELECT user_id, username FROM users")
for row in cursor:
    process_user(row)
```

**Pros:**
- No CSV needed
- Streaming (memory efficient)
- Always in sync
- Can filter: `WHERE is_bot=0` in one query

**Perf:** Negligible - DB queries are fast for 21k users

### 2. **Store profile download results in DB** (not JSON)

Add tracking columns to users table:

```sql
ALTER TABLE users ADD COLUMN (
    profile_photo_downloaded INTEGER DEFAULT 0,
    profile_photo_last_checked TIMESTAMP,
    profile_photo_count INTEGER DEFAULT 0,
    profile_photos_path TEXT
);
```

Then save downloads atomically:

```python
state.conn.execute('''
    UPDATE users 
    SET 
        profile_photo_downloaded = 1,
        profile_photo_last_checked = CURRENT_TIMESTAMP,
        profile_photo_count = ?
    WHERE user_id = ?
''', (count, user_id))
state.conn.commit()  # WAL ensures durability
```

**Pros:**
- Atomic, durable writes (WAL mode)
- Survives Ctrl+C
- Can query anytime: `SELECT * FROM users WHERE profile_photo_downloaded=0`
- No JSON file needed
- Recovery is built-in

### 3. **If you want backups, use proper database backup strategies**

Instead of JSON:

```python
# Option A: SQLite backup API (atomic, fast)
import shutil
backup_path = f"backups/users_analysis_{timestamp}.db"
shutil.copy2("data/users_analysis.db", backup_path)

# Option B: Scheduled external backup
# Use cron/scheduler to copy DB every N hours

# Option C: Periodic schema export (for portability)
# Export to CSV/JSON periodically for archival, not recovery
```

---

## Migration Path (Simple)

### Step 1: Update ProfilePhotoDownloader to query DB

```python
# Remove:
# self.csv_file_path = csv_file_path

# Add:
def load_users_from_db(self):
    cursor = self.state.conn.execute(
        "SELECT user_id, username, first_name, last_name FROM users"
    )
    return cursor  # Streaming = efficient
```

### Step 2: Add tracking columns to users table

```python
state = get_state_manager()
state.conn.execute('''
    ALTER TABLE users ADD COLUMN profile_photo_downloaded INTEGER DEFAULT 0
''')
state.conn.execute('''
    ALTER TABLE users ADD COLUMN profile_photo_last_checked TIMESTAMP
''')
```

### Step 3: Save downloads back to DB

```python
# When download succeeds:
state.conn.execute('''
    UPDATE users 
    SET profile_photo_downloaded=1, profile_photo_last_checked=CURRENT_TIMESTAMP
    WHERE user_id=?
''', (user_id,))
state.conn.commit()
```

---

## Why NOT To Use JSON Backups

| Aspect | SQLite WAL+Backup | JSON Backups |
| --- | --- | --- |
| **Durability** | ✅ Atomic writes on every change | ⚠️ Only when you call save() |
| **Recovery Speed** | ✅ Query immediately | ⚠️ Parse & reload entire file |
| **Query Support** | ✅ SQL filters, joins, aggregates | ❌ Must load all into memory |
| **Corruption Recovery** | ✅ SQLite has tools | ⚠️ JSON can't be repaired |
| **Space Efficiency** | ✅ Indexed, compressed | ❌ Flat text, duplicate data |
| **Interruption Safety** | ✅ Auto-committed changes | ❌ Manual commits easily missed |

---

## Summary: What You Should Do

1. **Kill CSV** - Direct DB queries for all features
2. **Kill JSON tracking** - Store profile metadata in DB
3. **Use DB backups** - SQLite backup or external backup tool, not JSON
4. **Keep JSON exports** - For portability only (one-way export)

This is  more resilient AND more efficient.
