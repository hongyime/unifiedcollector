"""Disposable PostgreSQL 16 database harness for recovery tests.

The harness has two jobs:

1. **Refuse production targets before any connection is made.**  A DSN is only
   considered disposable when it is loopback-only, uses a ``uc_recovery_test_*``
   database, carries an explicit fixture-ownership marker
   (``?uc_recovery_test=<run>``), and contains no ``unifiedcollector_*`` names or
   the production host port 5433.
2. **Own a throwaway database.**  When no external CI DSN is supplied, start one
   ``pgvector/pgvector:pg16`` container with a unique Compose-style
   project/container/volume/network name derived from a UUID, publish it on an
   ephemeral ``127.0.0.1`` port, and tear everything down in ``finally``.

Fixture contract (see ``conftest.py``):

* ``disposable_pg`` -- ``DisposablePostgres`` instance (session scoped).
* ``pg_dsn``       -- driver-safe DSN (the ownership marker is stripped).
* ``pg_conn``      -- open ``asyncpg.Connection`` for future restore/cursor/schema
  tests.

Environment overrides:

* ``UC_RECOVERY_TEST_DSN`` -- optional CI-provided disposable DSN.  It must pass
  the same guard (loopback + ``uc_recovery_test_*`` + ``?uc_recovery_test=...``
  marker).  Docker is not required in that mode.
* ``DATABASE_URL`` -- if present, it must equal the fixture DSN exactly; anything
  else is refused, even when it looks disposable.  This check does *not* choose
  the database; it only prevents a production or unrelated environment from
  being used accidentally.
* ``UC_RECOVERY_TEST_BOOT_TIMEOUT`` -- optional readiness timeout seconds.
* ``UC_RECOVERY_TEST_ALLOW_SKIP_NO_DOCKER=1`` -- explicit operator override that
  turns a missing Docker daemon/image into a reported skip instead of a failure.
  Without it, unavailable Docker is a hard failure.

The image is always used with ``--pull=never`` and the tag is pinned to
``pgvector/pgvector:pg16``; a missing image is a hard failure, never a skip.

Standalone guard::

    python3 tests/recovery/pg_harness.py --assert-disposable '<dsn>'

Exit 0 = accepted disposable DSN, exit 2 = refused.  The command does not
connect to the database and redacts the password from its output.
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit

IMAGE = "pgvector/pgvector:pg16"
TEST_LABEL = "uc.recovery.test"
RUN_LABEL = "uc.recovery.run"
TEST_LABEL_VALUE = "1"
OWNERSHIP_PARAM = "uc_recovery_test"
DB_PREFIX = "uc_recovery_test_"
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
FORBIDDEN_PORT = 5433
FORBIDDEN_SUBSTRINGS = ("unifiedcollector_pgdata", "unifiedcollector_postgres")
TARGET_OVERRIDE_PARAMS = frozenset(
    {"host", "hostaddr", "port", "dbname", "database", "service"}
)
DEFAULT_BOOT_TIMEOUT = 60.0


class DisposableDsnError(ValueError):
    """Raised when a DSN is not provably disposable; nothing was connected."""


class HarnessUnavailableError(RuntimeError):
    """Docker or the pinned image is unavailable; the caller must fail loudly."""


class HarnessTeardownError(RuntimeError):
    """Disposable resources could not be removed completely."""


# ---------------------------------------------------------------------------
# DSN guard
# ---------------------------------------------------------------------------
def disposable_dsn_reasons(dsn: str) -> list[str]:
    """Return every reason ``dsn`` is not acceptable as a disposable target."""
    reasons: list[str] = []
    raw = (dsn or "").strip()
    if not raw:
        return ["DSN is empty"]

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        return [f"DSN cannot be parsed: {exc}"]

    scheme = parts.scheme.lower()
    if scheme not in {"postgres", "postgresql"}:
        reasons.append(f"scheme must be postgres/postgresql, got {scheme or '<missing>'!r}")

    host = (parts.hostname or "").lower()
    if not host:
        reasons.append("DSN has no host")
    elif host not in ALLOWED_HOSTS:
        reasons.append(
            f"host {host!r} is not loopback; allowed hosts are {sorted(ALLOWED_HOSTS)}"
        )

    try:
        port = parts.port
    except ValueError:
        port = None
        reasons.append("DSN has an invalid port")
    if port == FORBIDDEN_PORT:
        reasons.append(f"port {FORBIDDEN_PORT} is reserved for the production PostgreSQL")
    elif port is not None and not (1 <= port <= 65535):
        reasons.append(f"port {port} is outside the valid range")

    database = unquote(parts.path.lstrip("/")).strip()
    if not database:
        reasons.append("DSN has no database name")
    else:
        lowered_db = database.lower()
        if "unifiedcollector" in lowered_db:
            reasons.append(f"database {database!r} references a production unifiedcollector name")
        if not lowered_db.startswith(DB_PREFIX):
            reasons.append(f"database {database!r} must start with {DB_PREFIX!r}")

    decoded = unquote(raw).lower()
    for token in FORBIDDEN_SUBSTRINGS:
        if token in decoded:
            reasons.append(f"DSN contains forbidden production name {token!r}")

    query = parse_qs(parts.query, keep_blank_values=True)
    marker_values = [value.strip() for value in query.get(OWNERSHIP_PARAM, []) if value.strip()]
    if not marker_values:
        reasons.append(
            f"DSN is not marked fixture-owned; add {OWNERSHIP_PARAM}=<run-id> to the query string"
        )
    for key in sorted(TARGET_OVERRIDE_PARAMS.intersection(query)):
        reasons.append(f"DSN must not override the target with query parameter {key!r}")

    return reasons


def assert_disposable_dsn(dsn: str) -> str:
    """Return ``dsn`` unchanged or raise :class:`DisposableDsnError`."""
    reasons = disposable_dsn_reasons(dsn)
    if reasons:
        raise DisposableDsnError("refusing non-disposable PostgreSQL DSN: " + "; ".join(reasons))
    return dsn


def is_disposable_dsn(dsn: str) -> bool:
    """True when :func:`assert_disposable_dsn` would accept ``dsn``."""
    return not disposable_dsn_reasons(dsn)


def strip_ownership_marker(dsn: str) -> str:
    """Remove the harness-only marker so database drivers never see it."""
    parts = urlsplit(dsn)
    pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != OWNERSHIP_PARAM]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(pairs, doseq=True), parts.fragment)
    )


def redact_dsn(dsn: str) -> str:
    """Render a DSN safe for logs/evidence: password and query values hidden."""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "<unparseable DSN>"
    user = parts.username or ""
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    netloc = f"{user}:***@{host}{port}" if user else f"{host}{port}"
    query_keys = sorted(parse_qs(parts.query, keep_blank_values=True))
    query = "&".join(f"{key}=***" for key in query_keys)
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def resolve_external_dsn() -> str | None:
    """Return a validated ``UC_RECOVERY_TEST_DSN`` or ``None``."""
    raw = os.environ.get("UC_RECOVERY_TEST_DSN", "").strip()
    if not raw:
        return None
    try:
        return assert_disposable_dsn(raw)
    except DisposableDsnError as exc:
        raise DisposableDsnError(f"UC_RECOVERY_TEST_DSN is not disposable: {exc}") from exc


def assert_environment_safe(fixture_dsn: str) -> None:
    """Refuse a ``DATABASE_URL`` that is not exactly the fixture DSN.

    The fixture never connects to ``DATABASE_URL``.  This is a pre-flight
    refusal so a production (or other unrelated) environment cannot silently
    run recovery tests.
    """
    current = os.environ.get("DATABASE_URL", "").strip()
    if not current or current == fixture_dsn:
        return
    reasons = disposable_dsn_reasons(current)
    detail = "; ".join(reasons) if reasons else "it is not the fixture-created disposable DSN"
    raise DisposableDsnError(
        "refusing to run recovery tests: DATABASE_URL is not the disposable fixture DSN "
        f"({redact_dsn(current)}): {detail}"
    )


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------
def _docker(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def docker_server_version() -> str | None:
    """Return the Docker server version, or ``None`` when unavailable."""
    try:
        completed = _docker(["version", "--format", "{{.Server.Version}}"], timeout=20.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = completed.stdout.strip()
    return version if completed.returncode == 0 and version else None


def local_image_id(image: str = IMAGE) -> str:
    """Return the local image id; fail loudly when the pinned image is absent."""
    completed = _docker(["image", "inspect", image, "--format", "{{.Id}}"], timeout=30.0)
    image_id = completed.stdout.strip()
    if completed.returncode != 0 or not image_id:
        detail = completed.stderr.strip() or completed.stdout.strip() or "docker image inspect failed"
        raise HarnessUnavailableError(
            f"required disposable PostgreSQL image {image!r} is not present locally "
            f"({detail}); refusing to skip. Pull/load it once and rerun -- the harness "
            "always uses --pull=never."
        )
    return image_id


def _not_found(stderr: str) -> bool:
    text = (stderr or "").lower()
    return any(token in text for token in ("no such", "not found", "does not exist"))


def list_test_resources(run_id: str | None = None) -> dict[str, list[str]]:
    """List containers/volumes/networks carrying the harness labels."""
    filters = [f"label={TEST_LABEL}={TEST_LABEL_VALUE}"]
    if run_id:
        filters.append(f"label={RUN_LABEL}={run_id}")

    def query(scope: str, *extra: str) -> list[str]:
        field = "{{.Names}}" if scope == "ps" else "{{.Name}}"
        completed = _docker([scope, *extra, *[item for f in filters for item in ("--filter", f)],
                             "--format", field], timeout=30.0)
        if completed.returncode != 0:
            return []
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

    return {
        "containers": query("ps", "-a"),
        "volumes": query("volume", "ls"),
        "networks": query("network", "ls"),
    }


def _remove(container: str, volume: str, network: str) -> list[str]:
    """Best-effort idempotent removal; returns human-readable problems."""
    problems: list[str] = []
    commands = [
        (["rm", "-f", container], "container"),
        (["volume", "rm", volume], "volume"),
        (["network", "rm", network], "network"),
    ]
    for args, kind in commands:
        try:
            completed = _docker(args, timeout=60.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{kind} cleanup failed: {exc}")
            continue
        if completed.returncode != 0 and not _not_found(completed.stderr):
            problems.append(
                f"{kind} cleanup failed ({' '.join(args)}): {completed.stderr.strip()}"
            )
    return problems


@dataclass(repr=False)
class DisposablePostgres:
    """A disposable PostgreSQL target plus the resources that own it.

    ``owned`` is true only when this process created the container, volume and
    network.  An external CI DSN reuses nothing and owns nothing.
    """

    dsn: str
    run_id: str
    owned: bool
    container_name: str = ""
    volume_name: str = ""
    network_name: str = ""
    database: str = ""
    user: str = ""
    host_port: int = 0
    image: str = IMAGE
    image_id: str = ""
    password: str = field(default="", repr=False)
    teardown_errors: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Never expose the generated password through pytest/assert output."""
        return (
            "DisposablePostgres("
            f"dsn={redact_dsn(self.dsn)!r}, run_id={self.run_id!r}, owned={self.owned!r}, "
            f"container_name={self.container_name!r}, volume_name={self.volume_name!r}, "
            f"network_name={self.network_name!r}, database={self.database!r}, "
            f"host_port={self.host_port!r}, image={self.image!r}"
            ")"
        )

    @property
    def driver_dsn(self) -> str:
        """DSN without the harness-only ownership query parameter."""
        return strip_ownership_marker(self.dsn)

    @classmethod
    def external(cls, dsn: str) -> "DisposablePostgres":
        """Wrap a CI-provided DSN that already passed :func:`assert_disposable_dsn`."""
        parts = urlsplit(dsn)
        marker = (parse_qs(parts.query).get(OWNERSHIP_PARAM) or ["external"])[0]
        return cls(
            dsn=dsn,
            run_id=f"external-{marker}",
            owned=False,
            database=unquote(parts.path.lstrip("/")),
            user=unquote(parts.username or ""),
            host_port=parts.port or 0,
        )

    @classmethod
    def start(cls, *, boot_timeout: float | None = None) -> "DisposablePostgres":
        """Start one uniquely named disposable container and wait until ready."""
        server_version = docker_server_version()
        if server_version is None:
            raise HarnessUnavailableError(
                "Docker is unavailable and no UC_RECOVERY_TEST_DSN was provided; "
                "refusing to skip recovery tests. Start Docker or set a disposable "
                "CI DSN that carries the ?uc_recovery_test=<run> marker."
            )
        image_id = local_image_id(IMAGE)
        timeout = boot_timeout or float(
            os.environ.get("UC_RECOVERY_TEST_BOOT_TIMEOUT", DEFAULT_BOOT_TIMEOUT)
        )

        run_id = uuid.uuid4().hex
        container = f"uc-recovery-test-{run_id}"
        volume = f"uc-recovery-test-{run_id}-data"
        network = f"uc-recovery-test-{run_id}-net"
        database = f"{DB_PREFIX}{run_id[:12]}"
        user = "uc_recovery_test"
        password = secrets.token_urlsafe(32)
        labels = [
            "--label", f"{TEST_LABEL}={TEST_LABEL_VALUE}",
            "--label", f"{RUN_LABEL}={run_id}",
            "--label", f"uc.recovery.image={IMAGE}",
        ]

        try:
            for args, kind in (
                (["network", "create", *labels, network], "network"),
                (["volume", "create", *labels, volume], "volume"),
            ):
                completed = _docker(args)
                if completed.returncode != 0:
                    raise HarnessUnavailableError(
                        f"could not create disposable {kind} {named(kind, network, volume)!r}: "
                        f"{completed.stderr.strip()}"
                    )
            completed = _docker(
                [
                    "run", "-d",
                    "--name", container,
                    *labels,
                    "--network", network,
                    "--network-alias", "pg",
                    "--publish", "127.0.0.1::5432",
                    "--volume", f"{volume}:/var/lib/postgresql/data",
                    "--env", f"POSTGRES_USER={user}",
                    "--env", f"POSTGRES_PASSWORD={password}",
                    "--env", f"POSTGRES_DB={database}",
                    "--pull=never",
                    IMAGE,
                ]
            )
            if completed.returncode != 0:
                raise HarnessUnavailableError(
                    "could not start disposable PostgreSQL container: "
                    f"{completed.stderr.strip()}"
                )

            host_port = _published_port(container)
            dsn = (
                f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
                f"@127.0.0.1:{host_port}/{quote(database, safe='')}"
                f"?{OWNERSHIP_PARAM}={quote(run_id, safe='')}"
            )
            harness = cls(
                dsn=dsn,
                run_id=run_id,
                owned=True,
                container_name=container,
                volume_name=volume,
                network_name=network,
                database=database,
                user=user,
                host_port=host_port,
                image=IMAGE,
                image_id=image_id,
                password=password,
            )
        except Exception:
            _remove(container, volume, network)
            raise

        try:
            harness.wait_ready(timeout)
        except Exception:
            harness.stop()
            raise
        return harness

    def wait_ready(self, timeout: float) -> None:
        """Poll ``pg_isready`` inside the container; fail with logs on timeout."""
        deadline = time.monotonic() + timeout
        last_status = "not probed"
        while time.monotonic() < deadline:
            running = _docker(["inspect", "--format", "{{.State.Running}}", self.container_name])
            if running.returncode != 0 or running.stdout.strip().lower() != "true":
                break
            completed = _docker(
                ["exec", self.container_name, "pg_isready",
                 "-h", "127.0.0.1", "-p", "5432",
                 "-U", self.user, "-d", self.database],
                timeout=20.0,
            )
            if completed.returncode == 0:
                return
            last_status = (completed.stderr or completed.stdout).strip() or "pg_isready failed"
            time.sleep(0.25)

        logs = _docker(["logs", "--tail", "80", self.container_name])
        raise HarnessUnavailableError(
            f"disposable PostgreSQL {self.container_name!r} did not become ready within "
            f"{timeout:g}s ({last_status}); container logs:\n"
            f"{(logs.stdout + logs.stderr).strip()}"
        )

    def stop(self) -> None:
        """Idempotently remove container, volume and network; report leftovers."""
        if not self.owned:
            return
        self.teardown_errors = _remove(
            self.container_name, self.volume_name, self.network_name
        )
        leftovers = list_test_resources(self.run_id)
        if any(leftovers.values()):
            self.teardown_errors.append(f"leftover disposable resources: {leftovers}")
        self.owned = False
        if self.teardown_errors:
            raise HarnessTeardownError(
                "disposable PostgreSQL teardown incomplete: "
                + "; ".join(self.teardown_errors)
            )


def named(kind: str, network: str, volume: str) -> str:
    """Small helper for error messages (kept separate to stay readable)."""
    return network if kind == "network" else volume


def _published_port(container: str) -> int:
    completed = _docker(["port", container, "5432/tcp"], timeout=30.0)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise HarnessUnavailableError(
            f"could not determine published port for {container!r}: {completed.stderr.strip()}"
        )
    line = completed.stdout.splitlines()[0].strip()
    host, _, port_text = line.rpartition(":")
    if host not in ALLOWED_HOSTS or not port_text.isdigit():
        raise HarnessUnavailableError(
            f"refusing unexpected published address {line!r}; expected loopback only"
        )
    return int(port_text)


# ---------------------------------------------------------------------------
# CLI: production-DSN guard
# ---------------------------------------------------------------------------
def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert that a PostgreSQL DSN targets a disposable recovery-test database."
    )
    parser.add_argument(
        "--assert-disposable",
        metavar="DSN",
        required=True,
        help="DSN to validate; exit 0 when disposable, 2 when refused",
    )
    args = parser.parse_args(argv)
    try:
        assert_disposable_dsn(args.assert_disposable)
    except DisposableDsnError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    print(f"ACCEPTED disposable DSN: {redact_dsn(args.assert_disposable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
