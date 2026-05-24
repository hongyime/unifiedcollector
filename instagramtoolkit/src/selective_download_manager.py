# Selective Download Manager - Manage custom download lists
import os
import json
from src.config import DATA_DIR
from src.io_utils import safe_json_write


def _fmt_count(n) -> str:
    """Format a follower count as human-readable: 45200 → '45.2k'."""
    if n is None:
        return "  ?"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _get_enriched_usernames(
    filter_vis: str = "all",      # "all" | "public" | "private"
    sort_by: str = "name",        # "name" | "followers" | "priority"
    search: str = "",             # substring filter on username
    limit: int = 0,               # 0 = no limit
) -> list[dict]:
    """Return tracked usernames enriched with profile metadata and download status.

    Each dict has keys:
        username, followers_count, is_public, media_count,
        download_status, following_accounts (list of account names)
    """
    import os as _os
    from db.manager import DatabaseManager
    db = DatabaseManager(_os.environ.get("DATABASE_URL", ""))

    rows = db.fetchall(
        """
        SELECT
            u.username,
            COALESCE(p.followers_count, -1)  AS followers_count,
            COALESCE(p.is_public, -1)        AS is_public,
            COALESCE(p.media_count, -1)      AS media_count,
            u.spider_status
        FROM usernames u
        LEFT JOIN profiles p ON p.username = u.username
        WHERE u.spider_status != 'inaccessible-all'
        ORDER BY u.username
        """
    )

    # Download status: check operation_progress for operation_id='download'
    dl_rows = db.fetchall(
        "SELECT username, status FROM operation_progress WHERE operation_id='download'"
    )
    dl_status = {r['username']: r['status'] for r in dl_rows}

    # Following accounts: group account_access rows
    access_rows = db.fetchall(
        "SELECT username, account_name FROM account_access WHERE follows=1"
    )
    following_map: dict[str, list[str]] = {}
    for ar in access_rows:
        following_map.setdefault(ar['username'], []).append(ar['account_name'])

    results = []
    for r in rows:
        uname = r['username']

        # search filter
        if search and search.lower() not in uname.lower():
            continue

        # visibility filter
        is_pub = r['is_public']
        if filter_vis == 'public' and is_pub == 0:
            continue
        if filter_vis == 'private' and is_pub != 0:
            continue

        results.append({
            'username': uname,
            'followers_count': r['followers_count'],
            'is_public': is_pub,
            'media_count': r['media_count'],
            'download_status': dl_status.get(uname, ''),
            'following_accounts': following_map.get(uname, []),
        })

    # sort
    if sort_by == 'followers':
        results.sort(key=lambda x: x['followers_count'] if x['followers_count'] >= 0 else -1, reverse=True)
    elif sort_by == 'priority':
        # priority = following > public > private
        def prio(x):
            has_follow = len(x['following_accounts']) > 0
            is_pub = x['is_public'] == 1
            return (0 if has_follow else 1, 0 if is_pub else 1, x['username'])
        results.sort(key=prio)
    # else: 'name' — already sorted by username from DB

    if limit > 0:
        results = results[:limit]
    return results


def _fmt_row(idx: int, item: dict, selected: bool, width_name: int = 22) -> str:
    """Render a single row in the selection table."""
    sel = "[x]" if selected else "[ ]"
    uname = item['username'][:width_name].ljust(width_name)

    fc = item['followers_count']
    followers = _fmt_count(fc).rjust(6) if fc >= 0 else "      ?"

    vis = item['is_public']
    pub = "pub  " if vis == 1 else ("PRIV " if vis == 0 else "  ?  ")

    ds = item['download_status']
    dl = "done  " if ds == 'completed' else ("FAIL  " if ds == 'failed' else "      ")

    accts = ",".join(item['following_accounts'][:3]) if item['following_accounts'] else ""
    accts = f"[{accts}]" if accts else "      "

    return f"{idx:4d} {sel} {uname} │{followers} │ {pub} │ {dl} │ {accts}"


class SelectiveDownloadManager:
    """Manages selective download lists for targeted media downloading."""

    def __init__(self):
        self.selective_list_file = os.path.join(DATA_DIR, 'selective_download_list.json')
        os.makedirs(DATA_DIR, exist_ok=True)
        self.selective_list = self._load_selective_list()

    def _load_selective_list(self):
        try:
            if os.path.exists(self.selective_list_file):
                with open(self.selective_list_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get('usernames', [])
            return []
        except Exception as e:
            print(f"[WARNING] Error loading selective list: {e}")
            return []

    def _save_selective_list(self):
        try:
            data = {
                'usernames': self.selective_list,
                'total_count': len(self.selective_list),
                'last_updated': self._get_timestamp(),
            }
            safe_json_write(self.selective_list_file, data)
            print(f"[OK] Selective list saved ({len(self.selective_list)} usernames)")
        except Exception as e:
            print(f"[ERROR] Failed to save selective list: {e}")

    def _get_timestamp(self):
        from datetime import datetime
        return datetime.now().isoformat()

    def get_available_usernames(self) -> list[str]:
        """Plain username list — uses UsernameRepository for backward compat."""
        try:
            import os as _os
            from db.repositories.username_repository import UsernameRepository
            from db.manager import DatabaseManager
            db = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
            rows = UsernameRepository(db).get_all()
            usernames = [r["username"] for r in rows]
            return usernames
        except Exception as e:
            print(f"[ERROR] Error loading usernames: {e}")
            return []

    def interactive_select(
        self,
        filter_vis: str = "all",
        sort_by: str = "name",
        search: str = "",
    ):
        """Interactive username selection with metadata display.

        filter_vis: 'all' | 'public' | 'private'
        sort_by:    'name' | 'followers' | 'priority'
        search:     substring filter
        """
        items = _get_enriched_usernames(filter_vis=filter_vis, sort_by=sort_by, search=search)
        if not items:
            print("[ERROR] No usernames match the current filter")
            return False

        selected_set = set(self.selective_list)
        chunk_size = 25

        print(f"\n=== Selective Download — Username Selection ===")
        print(f"   Filter={filter_vis}  Sort={sort_by}  Search={search!r}")
        print(f"   Available: {len(items)}   Selected: {len(selected_set)}")
        print()
        header = f"  #   S  {'Username':<22} │{'Flwrs':>6} │ Vis     │ Dwnld  │ Follows"
        print(header)
        print("─" * len(header))

        for chunk_start in range(0, len(items), chunk_size):
            chunk = items[chunk_start:chunk_start + chunk_size]
            for j, item in enumerate(chunk, chunk_start + 1):
                print(_fmt_row(j, item, item['username'] in selected_set))

            if chunk_start + chunk_size < len(items):
                nav = input(
                    f"\n[{chunk_start+1}–{chunk_start+len(chunk)}/{len(items)}] "
                    "Enter to continue, 's' to stop browsing: "
                ).strip().lower()
                if nav == 's':
                    break
            print()

        print("\n--- Selection (Enter numbers, ranges, or usernames) ---")
        print("   Examples: 1,5  |  10-20  |  username1,username2")
        print("   Commands: all | clear | done | show")

        while True:
            raw = input("\n> ").strip()
            if not raw:
                continue
            cmd = raw.lower()

            if cmd == 'done':
                break
            elif cmd == 'show':
                print(f"Selected ({len(selected_set)}): {', '.join(sorted(selected_set)[:20])}" +
                      ("..." if len(selected_set) > 20 else ""))
            elif cmd == 'clear':
                selected_set.clear()
                print("✅ Selection cleared")
            elif cmd == 'all':
                selected_set.update(item['username'] for item in items)
                print(f"✅ Selected all {len(selected_set)} matching usernames")
            else:
                added = removed = 0
                for part in raw.replace(' ', '').split(','):
                    # Toggle: prefix '-' removes
                    toggle_remove = part.startswith('-')
                    part = part.lstrip('-')

                    if '-' in part and part.replace('-', '').isdigit():
                        # range: 5-10
                        try:
                            a, b = part.split('-')
                            indices = range(int(a), int(b) + 1)
                        except ValueError:
                            continue
                        for idx in indices:
                            if 1 <= idx <= len(items):
                                u = items[idx - 1]['username']
                                if toggle_remove:
                                    selected_set.discard(u); removed += 1
                                else:
                                    selected_set.add(u); added += 1
                    elif part.isdigit():
                        idx = int(part)
                        if 1 <= idx <= len(items):
                            u = items[idx - 1]['username']
                            if toggle_remove:
                                selected_set.discard(u); removed += 1
                            elif u in selected_set:
                                selected_set.discard(u); removed += 1  # toggle off
                            else:
                                selected_set.add(u); added += 1
                    else:
                        # username string
                        if any(item['username'] == part for item in items):
                            if toggle_remove or part in selected_set:
                                selected_set.discard(part); removed += 1
                            else:
                                selected_set.add(part); added += 1
                        else:
                            print(f"  ⚠ Not found: {part}")

                if added or removed:
                    print(f"  +{added} / -{removed}  →  {len(selected_set)} selected")

        self.selective_list = sorted(selected_set)
        self._save_selective_list()
        print(f"\n[OK] Final Selection: {len(self.selective_list)} usernames")
        return True

    def _handle_number_selection(self, choice, available_usernames):
        try:
            for part in choice.split(','):
                part = part.strip()
                if '-' in part:
                    a, b = map(int, part.split('-'))
                    for n in range(a, b + 1):
                        if 1 <= n <= len(available_usernames):
                            u = available_usernames[n - 1]
                            if u not in self.selective_list:
                                self.selective_list.append(u)
                else:
                    n = int(part)
                    if 1 <= n <= len(available_usernames):
                        u = available_usernames[n - 1]
                        if u not in self.selective_list:
                            self.selective_list.append(u)
        except ValueError:
            print("❌ Invalid number format")

    def _handle_username_selection(self, choice, available_usernames):
        for username in [u.strip() for u in choice.split(',')]:
            if username in available_usernames and username not in self.selective_list:
                self.selective_list.append(username)

    def add_username(self, username):
        available = self.get_available_usernames()
        if username not in available:
            print(f"❌ Username '{username}' not found")
            return False
        if username in self.selective_list:
            print(f"ℹ  '{username}' already in selective list")
            return True
        self.selective_list.append(username)
        self._save_selective_list()
        print(f"✅ Added '{username}' to selective download list")
        return True

    def remove_username(self, username):
        if username in self.selective_list:
            self.selective_list.remove(username)
            self._save_selective_list()
            print(f"✅ Removed '{username}'")
            return True
        print(f"❌ '{username}' not in selective list")
        return False

    def clear_list(self):
        count = len(self.selective_list)
        self.selective_list = []
        self._save_selective_list()
        print(f"✅ Cleared {count} usernames")

    def show_list(self):
        print(f"\n🎯 Selective Download List ({len(self.selective_list)} usernames)")
        if not self.selective_list:
            print("📝 No usernames selected. Use 'selective-download --select'.")
            return
        items = _get_enriched_usernames()
        meta = {item['username']: item for item in items}
        header = f"  #   {'Username':<22} │{'Flwrs':>6} │ Vis     │ Dwnld  │ Follows"
        print(header)
        print("─" * len(header))
        for i, uname in enumerate(self.selective_list, 1):
            item = meta.get(uname, {
                'username': uname, 'followers_count': -1, 'is_public': -1,
                'media_count': -1, 'download_status': '', 'following_accounts': [],
            })
            print(_fmt_row(i, item, True))

    def get_selected_usernames(self):
        return self.selective_list.copy()

    def has_selection(self):
        return len(self.selective_list) > 0
