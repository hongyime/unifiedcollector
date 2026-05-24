"""Avatar downloader — downloads GitHub avatars by sequential user ID."""
import asyncio
import hashlib
from pathlib import Path
from typing import Optional, Set
import aiohttp
import aiosqlite

from src.config import Config
from src.database import save_avatar_download, get_downloaded_hashes


def scan_existing_ids(directory: Path) -> Set[int]:
    """
    One directory listing → set of integer user IDs already on disk.
    Handles {user_id}.jpg  (toolkit style)
          + {user_id}      (no extension — raw CDN download)
          + {user_id}.png  etc.
    Single readdir() call — safe for large network drives.
    """
    if not directory.exists():
        return set()
    ids: Set[int] = set()
    for p in directory.iterdir():
        if p.stem.isdigit():
            ids.add(int(p.stem))
    return ids


class AvatarDownloader:
    """Downloads GitHub avatars with async concurrency, disk-first dedup, and lazy DB sync."""

    def __init__(self, db_path: Path = Config.DB_PATH,
                 concurrency: int = 10,
                 save_dir: Optional[Path] = None):
        self.db_path = db_path
        self.concurrency = min(concurrency, Config.MAX_CONCURRENT_DOWNLOADS)
        self.save_dir = Path(save_dir) if save_dir else Config.AVATARS_DIR
        self.save_dir.mkdir(parents=True, exist_ok=True)

        self.session: Optional[aiohttp.ClientSession] = None
        self.semaphore: Optional[asyncio.Semaphore] = None

        # Populated in __aenter__
        self._disk_ids: Set[int] = set()   # IDs with file on disk (fast scan)
        self._db_ids: Set[int] = set()     # IDs already in avatar_downloads table
        self.hash_set: Set[str] = set()    # Content hashes in DB

        self.downloaded = 0
        self.skipped_on_disk = 0    # file present, DB record complete — nothing to do
        self.synced_to_db = 0       # file present, DB record added/updated
        self.errors = 0

    async def __aenter__(self):
        print(f"📂 Scanning: {self.save_dir}")
        self._disk_ids = scan_existing_ids(self.save_dir)
        print(f"   {len(self._disk_ids):,} files on disk")

        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            self.hash_set = await get_downloaded_hashes(db)
            cursor = await db.execute("SELECT user_id FROM avatar_downloads")
            self._db_ids = {row[0] for row in await cursor.fetchall()}
        print(f"   {len(self._db_ids):,} records in DB")

        untracked = len(self._disk_ids - self._db_ids)
        if untracked:
            print(f"   ⚠️  {untracked:,} files on disk not yet in DB — will sync lazily")

        self.semaphore = asyncio.Semaphore(self.concurrency)
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(limit=self.concurrency * 2),
            headers={'User-Agent': 'GitHub-Toolkit/2.0'})
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
        return False

    def _find_existing_file(self, user_id: int) -> Optional[Path]:
        """Find the actual file path for an id — checks .jpg then bare stem."""
        p = self.save_dir / f"{user_id}.jpg"
        if p.exists():
            return p
        p2 = self.save_dir / str(user_id)
        if p2.exists():
            return p2
        return None

    async def _sync_existing(self, user_id: int):
        """
        File is on disk but not in DB.
        Read it once, compute MD5, insert record. Skips the download entirely.
        Lazy: only runs for IDs actually encountered, not the whole directory.
        """
        filepath = self._find_existing_file(user_id)
        if not filepath:
            return  # shouldn't happen — disk_ids said it was there

        try:
            data = filepath.read_bytes()
            md5 = hashlib.md5(data).hexdigest()
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await save_avatar_download(db, user_id, md5, str(filepath))
                await db.commit()
            self._db_ids.add(user_id)
            self.hash_set.add(md5)
            self.synced_to_db += 1
        except Exception as e:
            print(f"   ⚠️  Could not sync {user_id}: {e}")

    async def download_avatar(self, user_id: int) -> bool:
        """
        Three cases:
        1. On disk + in DB  → fully tracked, skip (O(1))
        2. On disk, not DB  → lazy sync MD5+record, skip download
        3. Not on disk      → download, record
        """
        if user_id in self._disk_ids:
            if user_id in self._db_ids:
                self.skipped_on_disk += 1
                return True
            # Case 2: file exists but DB record missing
            await self._sync_existing(user_id)
            self.skipped_on_disk += 1
            return True

        # Case 3: download
        avatar_url = f"{Config.AVATAR_CDN_BASE}/{user_id}?s={Config.AVATAR_SIZE}"
        async with self.semaphore:
            try:
                async with self.session.get(avatar_url) as response:
                    if response.status != 200:
                        self.errors += 1
                        return False

                    data = await response.read()
                    md5 = hashlib.md5(data).hexdigest()

                    filepath = self.save_dir / f"{user_id}.jpg"
                    filepath.write_bytes(data)

                    async with aiosqlite.connect(self.db_path, timeout=30) as db:
                        await save_avatar_download(db, user_id, md5, str(filepath))
                        await db.commit()

                    self._disk_ids.add(user_id)
                    self._db_ids.add(user_id)
                    self.hash_set.add(md5)
                    self.downloaded += 1
                    return True

            except asyncio.TimeoutError:
                self.errors += 1
                return False
            except Exception:
                self.errors += 1
                return False

    async def download_range(self, start_id: int, end_id: int, delay: float = 0.5):
        """Download avatars for a sequential ID range."""
        print(f"🚀 Range: {start_id:,} → {end_id:,}  concurrency={self.concurrency}  delay={delay}s")
        current = start_id
        total = end_id - start_id + 1

        while current <= end_id:
            batch_end = min(current + self.concurrency - 1, end_id)
            await asyncio.gather(*[self.download_avatar(uid) for uid in range(current, batch_end + 1)])

            processed = batch_end - start_id + 1
            print(f"   {processed:,}/{total:,} ({processed/total*100:.1f}%) "
                  f"new={self.downloaded:,} skipped={self.skipped_on_disk:,} "
                  f"synced={self.synced_to_db:,} err={self.errors:,}")

            if delay > 0 and batch_end < end_id:
                await asyncio.sleep(delay)
            current = batch_end + 1

        print(f"✅ Done — new={self.downloaded:,} skipped={self.skipped_on_disk:,} "
              f"synced={self.synced_to_db:,} err={self.errors:,}")
