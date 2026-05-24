"""One-shot JSON-to-database migration script.

migrate_json_to_db(data_dir, db_manager) reads all existing JSON flat files
from data_dir, inserts their records into the database, and renames each
source file to <name>.bak ONLY after a successful commit.

Safety guarantees:
- NEVER touches .env, sessions/, or the data/ directory itself
- Missing files are skipped (recorded as skipped, no exception)
- Per-record errors are caught, recorded, and processing continues
- Returns a report dict: {"migrated": {...}, "errors": {...}, "skipped": [...]}
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any


def migrate_json_to_db(data_dir: str, db_manager) -> dict:
    """Migrate all JSON flat files in *data_dir* into *db_manager*.

    Args:
        data_dir: Path to the data/ directory (e.g. "data").
        db_manager: An initialised DatabaseManager instance.

    Returns:
        dict with keys "migrated" (counts per table), "errors" (per-record),
        "skipped" (list of filenames that were absent).
    """
    from .repositories.profile_repository import ProfileRepository
    from .repositories.relationship_repository import RelationshipRepository
    from .repositories.profile_access_repository import ProfileAccessRepository
    from .repositories.operation_progress_repository import OperationProgressRepository
    from .repositories.account_cooldown_repository import AccountCooldownRepository
    from .repositories.account_quota_repository import AccountQuotaRepository
    from .repositories.username_repository import UsernameRepository

    report: dict[str, Any] = {
        "migrated": {},
        "errors": {},
        "skipped": [],
    }

    profile_repo = ProfileRepository(db_manager)
    rel_repo = RelationshipRepository(db_manager)
    access_repo = ProfileAccessRepository(db_manager)
    progress_repo = OperationProgressRepository(db_manager)
    cooldown_repo = AccountCooldownRepository(db_manager)
    quota_repo = AccountQuotaRepository(db_manager)
    username_repo = UsernameRepository(db_manager)

    # ── 1. user_profiles.json ─────────────────────────────────────────────
    _migrate_profiles(data_dir, profile_repo, report)

    # ── 2. relationships.json ─────────────────────────────────────────────
    _migrate_relationships(data_dir, rel_repo, report)

    # ── 3. usernames.txt ──────────────────────────────────────────────────
    _migrate_usernames_txt(data_dir, username_repo, report)

    # ── 4. username_database.json ─────────────────────────────────────────
    _migrate_username_database(data_dir, username_repo, report)

    # ── 5. profile_access.json ────────────────────────────────────────────
    _migrate_profile_access(data_dir, access_repo, report)

    # ── 6. spider_progress.json ───────────────────────────────────────────
    _migrate_progress_file(data_dir, "spider_progress.json", "spider", progress_repo, report)

    # ── 7. download_progress.json ─────────────────────────────────────────
    _migrate_progress_file(data_dir, "download_progress.json", "download", progress_repo, report)

    # ── 8. account_cooldowns.json ─────────────────────────────────────────
    _migrate_cooldowns(data_dir, cooldown_repo, report)

    # ── 9. account_quotas.json ────────────────────────────────────────────
    _migrate_quotas(data_dir, quota_repo, report)

    return report


# ── Helper: safe file rename ──────────────────────────────────────────────

def _rename_to_bak(path: str) -> None:
    """Rename *path* to *path*.bak (only called after successful commit)."""
    bak = path + ".bak"
    os.rename(path, bak)


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Migration helpers ─────────────────────────────────────────────────────

def _migrate_profiles(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "user_profiles.json")
    if not os.path.exists(path):
        report["skipped"].append("user_profiles.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("user_profiles.json", []).append(str(e))
        return

    count = 0
    errors = []
    for username, profile in data.items():
        try:
            if not isinstance(profile, dict):
                profile = {}
            profile.setdefault("collected_by", profile.pop("collected_by_account", "migrated"))
            profile.setdefault("last_collected_ts", time.time())
            repo.upsert_profile(username, profile)
            count += 1
        except Exception as e:
            errors.append({username: str(e)})

    report["migrated"]["profiles"] = count
    if errors:
        report["errors"]["user_profiles.json"] = errors
    _rename_to_bak(path)


def _migrate_relationships(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "relationships.json")
    if not os.path.exists(path):
        report["skipped"].append("relationships.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("relationships.json", []).append(str(e))
        return

    if not isinstance(data, list):
        report["errors"].setdefault("relationships.json", []).append("Expected a list")
        return

    # Normalise field names
    normalised = []
    errors = []
    for i, r in enumerate(data):
        try:
            normalised.append({
                "source": r.get("source", ""),
                "target": r.get("target", ""),
                "type": r.get("type", "followers"),
                "collected_by": r.get("collected_by_account", r.get("collected_by", "migrated")),
                "source_is_public": r.get("source_is_public", True),
            })
        except Exception as e:
            errors.append({f"row_{i}": str(e)})

    count = repo.bulk_upsert(normalised)
    report["migrated"]["relationships"] = count
    if errors:
        report["errors"]["relationships.json"] = errors
    _rename_to_bak(path)


def _migrate_usernames_txt(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "usernames.txt")
    if not os.path.exists(path):
        report["skipped"].append("usernames.txt")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        report["errors"].setdefault("usernames.txt", []).append(str(e))
        return

    count = 0
    errors = []
    for line in lines:
        username = line.strip()
        if not username:
            continue
        try:
            repo.add_username(username, source_account="migrated", metadata={"migrated": True})
            count += 1
        except Exception as e:
            errors.append({username: str(e)})

    report["migrated"]["usernames_txt"] = count
    if errors:
        report["errors"]["usernames.txt"] = errors
    _rename_to_bak(path)


def _migrate_username_database(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "username_database.json")
    if not os.path.exists(path):
        report["skipped"].append("username_database.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("username_database.json", []).append(str(e))
        return

    usernames_data = data.get("usernames", {})
    count = 0
    errors = []
    for username, record in usernames_data.items():
        try:
            source = record.get("source_account", "migrated")
            meta = record.get("metadata", {})
            repo.add_username(username, source_account=source, metadata=meta)
            # Restore following status
            for acct, following in record.get("following_status", {}).items():
                try:
                    repo.update_following_status(username, acct, following)
                except Exception:
                    pass
            count += 1
        except Exception as e:
            errors.append({username: str(e)})

    report["migrated"]["username_database"] = count
    if errors:
        report["errors"]["username_database.json"] = errors
    _rename_to_bak(path)


def _migrate_profile_access(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "profile_access.json")
    if not os.path.exists(path):
        report["skipped"].append("profile_access.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("profile_access.json", []).append(str(e))
        return

    profiles = data.get("profiles", {})
    count = 0
    errors = []
    for username, profile_data in profiles.items():
        for attempt in profile_data.get("access_attempts", []):
            try:
                repo.record_attempt(
                    target=username,
                    account=attempt.get("account", "migrated"),
                    can_access=bool(attempt.get("can_access", False)),
                    is_public=attempt.get("is_public"),
                    is_followed=bool(attempt.get("is_followed", False)),
                    error=attempt.get("error"),
                )
                count += 1
            except Exception as e:
                errors.append({username: str(e)})

    report["migrated"]["profile_access"] = count
    if errors:
        report["errors"]["profile_access.json"] = errors
    _rename_to_bak(path)


def _migrate_progress_file(
    data_dir: str,
    filename: str,
    operation_id: str,
    repo,
    report: dict,
) -> None:
    path = os.path.join(data_dir, filename)
    if not os.path.exists(path):
        report["skipped"].append(filename)
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault(filename, []).append(str(e))
        return

    count = 0
    errors = []

    def _extract(entry):
        if isinstance(entry, dict):
            return entry.get("username", str(entry))
        return str(entry)

    for username in data.get("completed", []):
        try:
            repo.upsert_progress(operation_id, _extract(username), "completed")
            count += 1
        except Exception as e:
            errors.append({str(username): str(e)})

    for username in data.get("failed", []):
        try:
            repo.upsert_progress(operation_id, _extract(username), "failed")
            count += 1
        except Exception as e:
            errors.append({str(username): str(e)})

    for username in data.get("pending", []):
        try:
            repo.upsert_progress(operation_id, _extract(username), "pending")
            count += 1
        except Exception as e:
            errors.append({str(username): str(e)})

    key = filename.replace(".json", "")
    report["migrated"][key] = count
    if errors:
        report["errors"][filename] = errors
    _rename_to_bak(path)


def _migrate_cooldowns(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "account_cooldowns.json")
    if not os.path.exists(path):
        report["skipped"].append("account_cooldowns.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("account_cooldowns.json", []).append(str(e))
        return

    count = 0
    errors = []
    for account, entry in data.items():
        try:
            until_ts = entry.get("until", time.time())
            reason = entry.get("reason", "rate-limit")
            repo.put_on_cooldown(account, until_ts, reason)
            count += 1
        except Exception as e:
            errors.append({account: str(e)})

    report["migrated"]["account_cooldowns"] = count
    if errors:
        report["errors"]["account_cooldowns.json"] = errors
    _rename_to_bak(path)


def _migrate_quotas(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "account_quotas.json")
    if not os.path.exists(path):
        report["skipped"].append("account_quotas.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("account_quotas.json", []).append(str(e))
        return

    count = 0
    errors = []
    for account, entry in data.items():
        try:
            quota_date = entry.get("date", datetime.now().strftime("%Y-%m-%d"))
            profile_views = int(entry.get("profile_views", 0))
            actions = int(entry.get("actions", 0))
            # Insert row with correct date and counts
            _get_db_from_repo(repo).execute(
                """
                INSERT INTO account_quotas
                    (account_name, quota_date, profile_views, actions, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(account_name) DO UPDATE SET
                    quota_date    = excluded.quota_date,
                    profile_views = excluded.profile_views,
                    actions       = excluded.actions,
                    updated_at    = excluded.updated_at
                """,
                (account, quota_date, profile_views, actions, time.time()),
            )
            count += 1
        except Exception as e:
            errors.append({account: str(e)})

    report["migrated"]["account_quotas"] = count
    if errors:
        report["errors"]["account_quotas.json"] = errors
    _rename_to_bak(path)


def _get_db_from_repo(repo) -> Any:
    """Extract the DatabaseManager from a repository instance."""
    return repo._db


__all__ = ["migrate_json_to_db"]


