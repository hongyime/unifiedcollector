from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any, Iterable

from app_paths import APP_DATA_DIR, OAUTH_CREDENTIALS_FILE, TOKEN_FILE


def _candidate_paths() -> Iterable[Path]:
    seen: set[Path] = set()
    for path in (OAUTH_CREDENTIALS_FILE, TOKEN_FILE):
        if path not in seen:
            seen.add(path)
            yield path


def get_primary_credentials_path() -> Path:
    return OAUTH_CREDENTIALS_FILE


def load_cached_credentials() -> Any:
    for path in _candidate_paths():
        if not path.exists():
            continue

        with path.open("rb") as handle:
            credentials = pickle.load(handle)

        if path != OAUTH_CREDENTIALS_FILE:
            save_cached_credentials(credentials)

        return credentials

    return None


def save_cached_credentials(credentials: Any) -> Path:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with OAUTH_CREDENTIALS_FILE.open("wb") as handle:
        pickle.dump(credentials, handle)

    if TOKEN_FILE != OAUTH_CREDENTIALS_FILE and TOKEN_FILE.exists():
        TOKEN_FILE.unlink()

    return OAUTH_CREDENTIALS_FILE


def clear_cached_credentials() -> list[Path]:
    deleted_paths: list[Path] = []

    for path in _candidate_paths():
        if path.exists():
            path.unlink()
            deleted_paths.append(path)

    return deleted_paths
