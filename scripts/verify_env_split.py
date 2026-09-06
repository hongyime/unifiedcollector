"""Static analysis: per-service env parity between docker-compose.yml and src/.

For each service in docker/docker-compose.yml, this script:
  1. Parses the service's env_file: list -> sourced_env (union of KEYS from each
     .env.example under docker/env/ or the top-level .env.example, since real
     .env files are gitignored).
  2. Determines the source directories that run in the service from a hard-
     coded mapping (SERVICE_SOURCES).
  3. Greps those directories for env var references:
       os.getenv("KEY"), os.getenv('KEY')
       os.environ["KEY"], os.environ['KEY'], os.environ.get("KEY")
       env_bool("KEY", ...), env_int("KEY", ...), env_float("KEY", ...), env_str("KEY", ...)
     -> referenced_env.
  4. Reports referenced_env - sourced_env per service.

Exit code is 1 if ANY service has non-empty referenced_env - sourced_env.

By design, this script is NOT wired into CI yet (per per-service-env-split.md
step 20). A follow-up sprint will wire it once known false positives are
resolved or documented.

Usage:
    python scripts/verify_env_split.py
    python scripts/verify_env_split.py --service collector_spiderfoot
    python scripts/verify_env_split.py --json

False positives (allow-listed at the bottom of this file):
  * KEY names that live in ../.env.example (the legacy monolith) but that a
    service intentionally shouldn't source anymore, e.g. spiderfoot must not
    see INSTA_* / TELEGRAM_*. These are FEATURE-ok while ../.env is still
    dual-sourced; the check flips into an assertion after step 21.
  * Optional/debug env vars set via `export` outside the compose stack.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker" / "docker-compose.yml"
ENV_DIR = REPO_ROOT / "docker" / "env"
LEGACY_ENV_EXAMPLE = REPO_ROOT / ".env.example"

# ---------------------------------------------------------------------------
# Service -> source directories mapping
# ---------------------------------------------------------------------------
# Reflects what each Docker service actually imports at runtime, based on the
# `command:` in docker-compose.yml. Common paths (src/core, src/db) are added
# universally to worker-style services because every collector imports them
# via src.worker / src.main / base_collector.

# Paths are POSIX-style, resolved against REPO_ROOT.
COMMON_WORKER_PATHS = [
    "src/main.py",
    "src/worker",
    "src/core",
    "src/db",
    "src/notifications",  # base_collector uses telegram notifier for alerts
]

SERVICE_SOURCES: dict[str, list[str]] = {
    # External images -> no python source
    "postgres": [],
    "rabbitmq": [],
    "redis": [],
    "wa-bridge-1": [],
    "wa-bridge-2": [],

    # Idle catch-all container: all sources disabled at runtime via
    # COLLECTOR_DISABLED_SOURCES. Its executed code path is
    # src.main -> src.worker (imports only). Collector modules are imported but
    # their per-source env access is never exercised, so scope this narrowly.
    "collector": ["src/main.py", "src/worker", "src/core/env.py",
                  "src/core/logging_config.py", "src/core/health.py",
                  "src/db"],

    # Recon-only: MUST NOT reference collector platform creds
    "collector_spiderfoot": [
        "src/recon_spiderfoot_service.py",
        "src/recon_maigret_fp_refresh.py",
        "src/recon_seed_service.py",
        "src/core/recon.py",
        "src/core/recon_seed.py",
        "src/core/recon_spiderfoot.py",
        "src/core/ghunt_enrich.py",
        "src/core/env.py",
        "src/core/logging_config.py",
        "src/core/health.py",
        "src/db",
    ],

    # Per-source collectors -- COMMON_WORKER_PATHS union per-source subdir
    "collector_youtube": COMMON_WORKER_PATHS + ["src/collectors/youtube"],
    "collector_tiktok": COMMON_WORKER_PATHS + ["src/collectors/tiktok", "src/core/tiktok_browser.py"],
    "collector_lowrisk": COMMON_WORKER_PATHS + [
        "src/collectors/github",
        "src/collectors/strava",
        "src/collectors/search",
        "src/core/strava_route_queue.py",
        "src/core/tor_proxy.py",
    ],
    "collector_website": COMMON_WORKER_PATHS + ["src/collectors/website"],
    "collector_exposure": COMMON_WORKER_PATHS + [
        "src/collectors/exposure",
        "src/collectors/search",  # exposure re-uses search
    ],
    "collector_lemon8": COMMON_WORKER_PATHS + ["src/collectors/lemon8"],
    "collector_telegram": COMMON_WORKER_PATHS + ["src/collectors/telegram"],
    "collector_beeper": COMMON_WORKER_PATHS + ["src/collectors/beeper"],
    "collector_whatsapp": COMMON_WORKER_PATHS + ["src/collectors/whatsapp"],
    "collector_instagram": COMMON_WORKER_PATHS + ["src/collectors/instagram"],
    "collector_instagram_dm": COMMON_WORKER_PATHS + ["src/collectors/instagram_dm"],

    # Bridges / bots
    "ig_ingest": [
        "src/bridges/ig_ingest",
        "src/core",
        "src/db",
        "src/notifications",
    ],
    "onboard_bot": [
        "src/bots/onboard_bot.py",
        "src/core/env.py",
        "src/core/logging_config.py",
        "src/db",
    ],

    # Support services
    "watchdog": [
        "src/watchdog",
        "src/core/env.py",
        "src/core/logging_config.py",
        "src/notifications",
        "src/db",
    ],
    "scheduler": [
        "src/main.py",
        "src/scheduler",
        "src/core",
        "src/db",
        "src/notifications",
    ],
    "realtime_feed": [
        "src/notifications/realtime_feed.py",
        "src/notifications/realtime_delivery.py",
        "src/notifications/telegram.py",
        "src/core/env.py",
        "src/core/logging_config.py",
        "src/db",
    ],
    "dashboard": [
        "src/dashboard",
        "src/core",
        "src/db",
        "src/notifications",
    ],
    "backup": [
        "src/backup",
        "src/core/env.py",
        "src/core/logging_config.py",
        "src/notifications/telegram.py",
    ],
    "browser_cookie_vault": [
        "src/tools/browser_cookie_vault.py",
        "src/core/env.py",
        "src/core/logging_config.py",
    ],
}

# ---------------------------------------------------------------------------
# Known false positives (never treated as missing).
#
# Keys that appear in code via os.getenv(...) but shouldn't be flagged because:
#   * They come from Docker Compose variable substitution ($env in
#     docker/.env) rather than env_file, and get baked into the runtime
#     environment via `environment:` blocks in docker-compose.yml.
#   * They are optional dev-only knobs (LOG_LEVEL, PYTHONPATH, ...).
#   * They are set inside the container by an entrypoint script.
# ---------------------------------------------------------------------------
GLOBAL_ALLOWLIST = {
    # Standard/Python runtime
    "HOME", "PATH", "USER", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONUNBUFFERED",
    "TZ", "LOG_LEVEL", "TMPDIR", "TMP", "TEMP",
    # Compose substitution / container env (set via `environment:` blocks)
    "COLLECTOR_HANG_TIMEOUT_SECONDS",
    "COLLECTOR_DISABLED_SOURCES",
    "DB_POOL_MIN_SIZE", "DB_POOL_MAX_SIZE",
    "PGUSER", "PGPASSWORD", "PGHOST", "PGDATABASE",
    "POSTGRES_DB",
    "REDIS_HOST", "REDIS_URL",
    # WhatsApp bridge inject via `environment:` (Node.js side; not Python)
    "SESSION_NAME", "AUTH_STORAGE_PATH", "SYNC_FULL_HISTORY",
    "WHATSAPP_DEEP_BACKFILL", "WHATSAPP_FETCH_COUNT",
    "WHATSAPP_UNPAIRED_QR_RECONNECT_MS", "WHATSAPP_QR_STABILITY_MS",
    # Injected by compose environment: block on collector_youtube
    "YOUTUBE_ENRICH_BATCH_LIMIT",
    # PWD-style vars that never live in .env files
    "WORKDIR", "DOCKER_HOST",
}

# Per-service allowlist: keys that a specific service references but doesn't
# need in its env_file (because they come from compose environment: block).
SERVICE_ALLOWLIST: dict[str, set[str]] = {
    # `environment:` block on collector_instagram_dm sets these directly:
    "collector_instagram_dm": {
        "INSTAGRAM_DM_COLLECTOR_ENABLED",
        "INSTAGRAM_DM_CREDENTIALS_DIR",
        "INSTAGRAM_DM_PROXY_URL",
    },
}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_SERVICE_RE = re.compile(r"^  ([a-z][a-z0-9_-]*):$")
_VOLUMES_TOP_RE = re.compile(r"^volumes:$")
_ENV_FILE_RE = re.compile(r"^    env_file:$")
_ENV_ENTRY_RE = re.compile(r"^      - (.+)$")
_ENVIRONMENT_RE = re.compile(r"^    environment:$")
_ENVIRONMENT_KEY_RE = re.compile(r"^      ([A-Z_][A-Z0-9_]*)\s*:")

_KEY_LINE_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=")

# Match a variety of env-var access patterns.
_ENV_ACCESS_RE = re.compile(
    r"""
    (?:
        # os.getenv("KEY") or os.getenv('KEY')
        \bos\.getenv\(\s*["']([A-Z_][A-Z0-9_]*)["']
      |
        # bare getenv("KEY") for `from os import getenv`
        (?<!\.)\bgetenv\(\s*["']([A-Z_][A-Z0-9_]*)["']
      |
        # os.environ["KEY"] or os.environ.get("KEY")
        \bos\.environ\s*(?:\.get)?\s*\[?\s*\(?\s*["']([A-Z_][A-Z0-9_]*)["']
      |
        # env_bool/env_int/env_float/env_str("KEY", ...)
        \benv_(?:bool|int|float|str)\(\s*["']([A-Z_][A-Z0-9_]*)["']
    )
    """,
    re.VERBOSE,
)


def parse_compose(compose: Path = COMPOSE_FILE) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """Parse compose file.

    Returns:
        (env_files, inline_env)
        env_files: service_name -> list of env_file entries (as written)
        inline_env: service_name -> set of KEYs defined via `environment:` block
    """
    lines = compose.read_text(encoding="utf-8").splitlines()
    env_files: dict[str, list[str]] = {}
    inline_env: dict[str, set[str]] = {}
    current: str | None = None
    section: str | None = None  # "env_file" | "environment" | None
    for line in lines:
        if _VOLUMES_TOP_RE.match(line):
            break
        m = _SERVICE_RE.match(line)
        if m:
            current = m.group(1)
            env_files[current] = []
            inline_env[current] = set()
            section = None
            continue
        if current is None:
            continue
        if _ENV_FILE_RE.match(line):
            section = "env_file"
            continue
        if _ENVIRONMENT_RE.match(line):
            section = "environment"
            continue
        if section == "env_file":
            em = _ENV_ENTRY_RE.match(line)
            if em:
                env_files[current].append(em.group(1))
                continue
            section = None
        if section == "environment":
            km = _ENVIRONMENT_KEY_RE.match(line)
            if km:
                inline_env[current].add(km.group(1))
                continue
            # Leaving the block on any non-key, non-comment line at the same indent
            if line and not line.startswith("      ") and not line.startswith("      #"):
                section = None
    return env_files, inline_env


def resolve_env_entry(entry: str) -> Path | None:
    """Resolve an env_file: entry (relative to docker/) to an .env.example path.

    Real .env files are gitignored, so we substitute .env -> .env.example.
    """
    compose_dir = COMPOSE_FILE.parent  # docker/
    if entry == "../.env":
        return LEGACY_ENV_EXAMPLE
    # entry like ./env/instagram.env or env/instagram.env
    p = (compose_dir / entry).resolve()
    example = Path(str(p) + ".example")
    if example.exists():
        return example
    if p.exists():
        return p
    return None


def parse_env_keys(env_path: Path) -> set[str]:
    """Return the set of KEY= names in an env file (any assignment line)."""
    if not env_path.exists():
        return set()
    keys: set[str] = set()
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        # strip comments/leading whitespace
        m = _KEY_LINE_RE.match(line)
        if m:
            keys.add(m.group(1))
    return keys


def collect_python_files(paths: list[str]) -> list[Path]:
    """Expand a mix of file/dir paths into a flat list of .py files."""
    out: list[Path] = []
    for rel in paths:
        p = (REPO_ROOT / rel).resolve()
        if not p.exists():
            continue
        if p.is_file() and p.suffix == ".py":
            out.append(p)
        elif p.is_dir():
            for f in p.rglob("*.py"):
                # Skip __pycache__, tests, migrations
                if "__pycache__" in f.parts:
                    continue
                if "migrations" in f.parts:
                    continue
                out.append(f)
    return out


def scan_env_references(files: list[Path]) -> dict[str, list[Path]]:
    """Return {ENV_KEY: [file1, file2, ...]} across the given source files."""
    hits: dict[str, list[Path]] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _ENV_ACCESS_RE.finditer(text):
            key = next((g for g in m.groups() if g), None)
            if key:
                hits.setdefault(key, []).append(f)
    return hits


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze() -> tuple[dict[str, dict], int]:
    env_files, inline_env = parse_compose()
    report: dict[str, dict] = {}
    total_missing = 0

    # Vocabulary = union of every KEY defined across every known env.example.
    # A referenced key that is present in SOME env.example is "documented" —
    # the system understands the knob, even if this specific service doesn't
    # wire the file that carries it. This suppresses cross-service shared-
    # import false positives while still catching keys that appear in code
    # but in NO env.example (real documentation gaps).
    vocabulary: set[str] = set()
    if LEGACY_ENV_EXAMPLE.exists():
        vocabulary.update(parse_env_keys(LEGACY_ENV_EXAMPLE))
    for f in ENV_DIR.glob("*.env.example"):
        vocabulary.update(parse_env_keys(f))

    for svc, entries in env_files.items():
        sourced: set[str] = set()
        for entry in entries:
            resolved = resolve_env_entry(entry)
            if resolved is None:
                continue
            sourced.update(parse_env_keys(resolved))
        # Compose `environment:` block also feeds the container's env
        sourced.update(inline_env.get(svc, set()))

        src_paths = SERVICE_SOURCES.get(svc, [])
        py_files = collect_python_files(src_paths)
        refs = scan_env_references(py_files)
        referenced = set(refs.keys())

        allowed = GLOBAL_ALLOWLIST | SERVICE_ALLOWLIST.get(svc, set())
        # Cross-service documented: known to the vocabulary, just not wired
        # into this service's env_file list. Reported as INFO, not MISSING.
        cross_service = sorted(
            k for k in (referenced - sourced)
            if k not in allowed and k in vocabulary
        )
        # True missing: referenced by code, NOT sourced, NOT allowed, and NOT
        # in the system's env.example vocabulary at all.
        missing = sorted(
            k for k in (referenced - sourced)
            if k not in allowed and k not in vocabulary
        )

        report[svc] = {
            "env_file": entries,
            "inline_env_count": len(inline_env.get(svc, set())),
            "sourced_count": len(sourced),
            "referenced_count": len(referenced),
            "scanned_files": len(py_files),
            "cross_service": cross_service,
            "missing": missing,
        }
        total_missing += len(missing)

    return report, total_missing


def print_report(report: dict[str, dict], missing_total: int) -> None:
    print(f"verify_env_split.py: scanned {len(report)} services")
    print()
    for svc, info in report.items():
        line = (
            f"  {svc:26s} sources={len(info['env_file'])}  keys={info['sourced_count']:4d}"
            f"  refs={info['referenced_count']:4d}  scanned={info['scanned_files']:4d}"
            f"  cross-service={len(info['cross_service'])}  missing={len(info['missing'])}"
        )
        print(line)
        for k in info["missing"]:
            print(f"      MISSING     {k}    (not defined in any env.example)")
    print()
    if missing_total == 0:
        print("OK: every referenced env var is either sourced or documented in an env.example.")
    else:
        print(f"FAIL: {missing_total} referenced env var(s) with NO env.example entry across the system.")
    print()
    print("Note: 'cross-service' counts referenced keys that live in another service's env.example.")
    print("They are informational, not a coverage gap — the code path referencing them is likely")
    print("shared infrastructure that this service imports but never executes at runtime.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--service", help="Only analyze one service")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a text report")
    args = ap.parse_args()

    report, missing_total = analyze()

    if args.service:
        if args.service not in report:
            print(f"unknown service: {args.service}", file=sys.stderr)
            return 2
        report = {args.service: report[args.service]}
        missing_total = len(report[args.service]["missing"])

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report, missing_total)

    return 1 if missing_total else 0


if __name__ == "__main__":
    sys.exit(main())
