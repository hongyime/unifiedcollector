"""CLI Helper utilities for Unified Instagram Toolkit.

Centralizes common helper logic used by the main CLI entrypoint to reduce
duplication and keep `main.py` focused on argument parsing and command routing.
"""
from __future__ import annotations

from typing import List, Optional
import os

from src.config import get_account_by_name, get_default_account


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


def load_usernames() -> List[str]:
    """Load usernames from the database.

    Returns an empty list if no usernames are tracked yet.
    """
    try:
        from db.repositories.username_repository import UsernameRepository
        rows = UsernameRepository(_get_db()).get_all()
        usernames = [r["username"] for r in rows]
        if not usernames:
            print("[ERROR] No usernames found in database. Add usernames with: python main.py add-username <name>")
        return usernames
    except Exception as e:
        print(f"[ERROR] Failed to load usernames from database: {e}")
        return []


def resolve_account_config(account_name: Optional[str]):
    """Resolve an account configuration by name (or default if None).

    Prints user-friendly error messages on failure and returns None.
    """
    account_config = get_account_by_name(account_name) if account_name else get_default_account()
    if not account_config:
        print("[ERROR] No valid account found (check config.py)")
        return None
    return account_config


def get_account_username(account_name: Optional[str]) -> Optional[str]:
    """Convenience wrapper returning the Instagram username for a configured account."""
    account = resolve_account_config(account_name)
    if account:
        return account.get("username")
    return None


__all__ = [
    "load_usernames",
    "resolve_account_config",
    "get_account_username",
]


