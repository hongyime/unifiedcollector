"""Database-to-disk reconciliation for avatar downloads."""
import asyncio
from pathlib import Path
import aiosqlite

from src.config import Config
from src.avatar_downloader import AvatarDownloader


class Reconciler:
    """Reconciles database records with filesystem."""

    def __init__(self, db_path: Path = Config.DB_PATH, avatars_dir=None):
        """Initialize reconciler.

        Args:
            db_path: Path to database
            avatars_dir: Directory where avatars are stored (defaults to Config.AVATARS_DIR)
        """
        self.db_path = db_path
        self.avatars_dir = Path(avatars_dir) if avatars_dir else Config.AVATARS_DIR
    
    async def reconcile_avatars(self):
        """Reconcile avatar downloads - re-download missing files."""
        print("🔄 Starting avatar reconciliation...")
        
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            # Get all avatar download records
            cursor = await db.execute("""
                SELECT user_id, file_path FROM avatar_downloads
                WHERE file_path IS NOT NULL AND file_path != ''
            """)
            
            missing = []
            total = 0
            
            async for row in cursor:
                total += 1
                user_id = row[0]
                file_path = Path(row[1])
                
                if not file_path.exists():
                    missing.append(user_id)
            
            print(f"📊 Reconciliation scan complete:")
            print(f"   Total records: {total:,}")
            print(f"   Missing files: {len(missing):,}")
            
            if not missing:
                print("✅ All files present!")
                return
            
            # Re-download missing files
            print(f"🔄 Re-downloading {len(missing):,} missing avatars...")
            
            async with AvatarDownloader(self.db_path, concurrency=10, save_dir=self.avatars_dir) as downloader:
                for i, user_id in enumerate(missing, 1):
                    await downloader.download_avatar(user_id)
                    
                    if i % 100 == 0:
                        print(f"   Progress: {i:,}/{len(missing):,}")
            
            print(f"✅ Reconciliation complete!")
            print(f"   Re-downloaded: {downloader.downloaded}")
            print(f"   Errors: {downloader.errors}")
    
    async def verify_integrity(self) -> dict:
        """Verify database integrity.
        
        Returns:
            Dict with integrity check results
        """
        print("🔍 Verifying database integrity...")
        
        results = {
            'orphaned_edges': 0,
            'missing_user_ids': 0,
            'duplicate_edges': 0
        }
        
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            # Check for orphaned edges (edges pointing to non-existent users)
            cursor = await db.execute("""
                SELECT COUNT(*) FROM graph_edges
                WHERE source_username NOT IN (SELECT username FROM users)
                   OR target_username NOT IN (SELECT username FROM users)
            """)
            results['orphaned_edges'] = (await cursor.fetchone())[0]
            
            # Check for users without user_id
            cursor = await db.execute("""
                SELECT COUNT(*) FROM users WHERE user_id IS NULL
            """)
            results['missing_user_ids'] = (await cursor.fetchone())[0]
            
            # Check for duplicate edges
            cursor = await db.execute("""
                SELECT source_username, target_username, COUNT(*) as cnt
                FROM graph_edges
                GROUP BY source_username, target_username
                HAVING cnt > 1
            """)
            duplicates = await cursor.fetchall()
            results['duplicate_edges'] = len(duplicates)
        
        print(f"📊 Integrity check results:")
        print(f"   Orphaned edges: {results['orphaned_edges']}")
        print(f"   Missing user IDs: {results['missing_user_ids']}")
        print(f"   Duplicate edges: {results['duplicate_edges']}")
        
        if sum(results.values()) == 0:
            print("✅ Database integrity OK!")
        else:
            print("⚠️  Issues found. Consider running cleanup.")
        
        return results
