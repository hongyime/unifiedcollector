# User data analysis: compute follower/following counts and summary reports
import os
import csv
from src.config import DATA_DIR
from src.io_utils import safe_json_write


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class UserAnalyzer:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.usernames = self._load_usernames()
        self.relationships = self._load_relationships()

    def _load_usernames(self):
        try:
            from db.repositories.username_repository import UsernameRepository
            rows = UsernameRepository(_get_db()).get_all()
            usernames = [r["username"] for r in rows]
            print(f"Loaded {len(usernames)} usernames for analysis")
            return usernames
        except Exception as e:
            print(f"Error loading usernames: {e}")
            return []

    def _load_relationships(self):
        try:
            from db.repositories.relationship_repository import RelationshipRepository
            rows = RelationshipRepository(_get_db()).get_relationships()
            print(f"Loaded {len(rows)} relationships for analysis")
            return rows
        except Exception as e:
            print(f"Error loading relationships: {e}")
            return []

    def analyze(self):
        # Initialize stats for known usernames
        default_entry = lambda: {'followers_count': 0, 'following_count': 0}
        stats = {u: default_entry() for u in self.usernames}
        # Count relationships
        for rel in self.relationships:
            src = rel.get('source')
            tgt = rel.get('target')
            typ = rel.get('type')
            if not src:
                continue
            # Auto-create entry for sources not in usernames table
            if src not in stats:
                stats[src] = default_entry()
            if typ == 'followers':
                stats[src]['followers_count'] += 1
            elif typ == 'following':
                stats[src]['following_count'] += 1
        return stats

    def save_json(self, path):
        """Write analysis to a JSON file (optional export)."""
        try:
            summary = self.analyze()
            safe_json_write(path, summary)
            print(f"Saved JSON report to {path}")
        except Exception as e:
            print(f"Error saving JSON report: {e}")

    def save_csv(self, path):
        """Write analysis to a CSV file (optional export)."""
        try:
            summary = self.analyze()
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['username', 'followers_count', 'following_count'])
                for u, s in summary.items():
                    writer.writerow([u, s['followers_count'], s['following_count']])
            print(f"Saved CSV report to {path}")
        except Exception as e:
            print(f"Error saving CSV report: {e}")

    def print_summary(self):
        """Print a quick summary directly from the DB — no file I/O needed."""
        try:
            db = _get_db()
            total_users = db.fetchone("SELECT COUNT(*) as cnt FROM usernames")
            total_rels = db.fetchone("SELECT COUNT(*) as cnt FROM relationships")
            followers = db.fetchone("SELECT COUNT(*) as cnt FROM relationships WHERE type='followers'")
            following = db.fetchone("SELECT COUNT(*) as cnt FROM relationships WHERE type='following'")
            top = db.fetchall(
                "SELECT source, COUNT(*) as cnt FROM relationships GROUP BY source ORDER BY cnt DESC LIMIT 10"
            )

            print()
            print("=" * 50)
            print("  Network Analysis Summary")
            print("=" * 50)
            print(f"  Tracked usernames : {total_users['cnt'] if total_users else 0:,}")
            print(f"  Total relationships: {total_rels['cnt'] if total_rels else 0:,}")
            print(f"    Follower links   : {followers['cnt'] if followers else 0:,}")
            print(f"    Following links  : {following['cnt'] if following else 0:,}")
            if top:
                print()
                print("  Top 10 most-connected users:")
                for i, r in enumerate(top, 1):
                    print(f"    {i:2d}. {r['source']} ({r['cnt']} relationships)")
            print("=" * 50)
            print()
        except Exception as e:
            print(f"[ERROR] Could not read analysis from DB: {e}")


