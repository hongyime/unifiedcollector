"""Verify the value-free credential-to-consumer map used for recovery.

The verifier reads only tracked ``*.env.example`` files and Compose wiring. It
never opens a real env file and never prints a credential value.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.verify_env_split import ENV_DIR, REPO_ROOT, parse_compose, parse_env_keys, resolve_env_entry

MAP_PATH = REPO_ROOT / "config" / "credential-consumer-map.json"
_CREDENTIAL_SUFFIX = re.compile(
    r"(?:_PASSWORD|_TOKEN|_API_KEY(?:S)?|_CLIENT_ID|_CLIENT_SECRET|"
    r"_COOKIES_FILE|_SESSION_COOKIE|_REFRESH_TOKEN|_OAUTH_PICKLE|_SECRET)$"
)
_EXTRA_CREDENTIALS = {
    "GITHUB_PAT", "TELEGRAM_BOT_TOKENS", "YOUTUBE_COOKIE_BROWSER", "YOUTUBE_COOKIE_FILE",
}
_SCOPES = {"required", "optional", "human-controlled"}


def candidate_credentials(env_dir: Path = ENV_DIR) -> dict[str, set[str]]:
    """Return credential names and their documented example-file sources."""
    candidates: dict[str, set[str]] = {}
    for path in sorted(env_dir.glob("*.env.example")):
        relative = str(path.relative_to(REPO_ROOT))
        for name in parse_env_keys(path):
            if _CREDENTIAL_SUFFIX.search(name) or name in _EXTRA_CREDENTIALS:
                candidates.setdefault(name, set()).add(relative)
    return candidates


def effective_consumers(selector: dict[str, Any], env_files: dict[str, list[str]]) -> set[str]:
    """Resolve a map consumer selector against tracked Compose env-file wiring."""
    if set(selector) == {"services"} and isinstance(selector["services"], list):
        if not all(isinstance(service, str) and service for service in selector["services"]):
            raise ValueError("services selector must be a nonempty string list")
        return set(selector["services"])
    if set(selector) == {"env_file"} and isinstance(selector["env_file"], str):
        entry = selector["env_file"]
        return {service for service, entries in env_files.items() if entry in entries}
    raise ValueError("consumers must select explicit services or one env_file")


def load_map(path: Path = MAP_PATH) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read credential map: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported credential map schema")
    if document.get("live_presence") != "not_inspected":
        raise ValueError("credential map must state that live values were not inspected")
    credentials = document.get("credentials")
    if not isinstance(credentials, list):
        raise ValueError("credential map must contain a credentials list")
    return document


def verify_map(document: dict[str, Any], *, candidates: dict[str, set[str]] | None = None) -> list[str]:
    """Return value-free validation errors for a credential map document."""
    candidates = candidate_credentials() if candidates is None else candidates
    env_files, _inline_env = parse_compose()
    rows = document.get("credentials") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return ["credential map must contain a credentials list"]

    errors: list[str] = []
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            errors.append("credential map row is missing a name")
            continue
        name = row["name"]
        if name in by_name:
            errors.append(f"duplicate credential row: {name}")
            continue
        by_name[name] = row
        if row.get("scope") not in _SCOPES:
            errors.append(f"invalid scope: {name}")
        source = row.get("source")
        if not isinstance(source, str) or source not in candidates.get(name, set()):
            errors.append(f"incorrect example source: {name}")
        if not isinstance(row.get("validation"), str) or name not in row["validation"]:
            errors.append(f"missing value-free validation command: {name}")
        if not isinstance(row.get("dependency"), str) or not row["dependency"].strip():
            errors.append(f"missing dependency: {name}")
        selector = row.get("consumers")
        if not isinstance(selector, dict):
            errors.append(f"missing consumer selector: {name}")
            continue
        try:
            listed = effective_consumers(selector, env_files)
        except ValueError as exc:
            errors.append(f"invalid consumer selector for {name}: {exc}")
            continue
        expected = {
            service for service, entries in env_files.items()
            if any(
                (resolved := resolve_env_entry(entry)) is not None
                and str(resolved.relative_to(REPO_ROOT)) == source
                for entry in entries
            )
        }
        if listed != expected:
            errors.append(f"consumer coverage mismatch: {name}")

    for name in sorted(candidates):
        if name not in by_name:
            errors.append(f"unmapped credential: {name}")
    for name in sorted(set(by_name) - set(candidates)):
        errors.append(f"map contains undocumented credential: {name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--map", type=Path, default=MAP_PATH)
    parser.add_argument("--name", help="Print one value-free credential row")
    args = parser.parse_args()
    try:
        document = load_map(args.map)
        errors = verify_map(document)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    rows = {row["name"]: row for row in document["credentials"]}
    if args.name:
        row = rows.get(args.name)
        if row is None:
            print(f"FAIL: unknown credential: {args.name}")
            return 2
        print(json.dumps(row, sort_keys=True))
    else:
        print(f"OK: {len(rows)} documented credentials; no values read")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
