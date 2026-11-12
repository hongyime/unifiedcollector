"""Source-owned gallery-dl/yt-dlp fallback jobs.

This deliberately is not a global downloader.  A collector enqueues a
canonical post/profile URL only after its native retry budget is exhausted;
the same source's worker claims and completes the job.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from src.core.subprocess_downloader import gallery_dl_download, managed_tempdir, yt_dlp_download
from src.core.vault import VAULT_ROOT, write_atomic_artifact_from_path

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class FallbackCapability:
    """The complete, source-owned contract for a fallback capability."""

    source: str
    owner: str
    kinds: frozenset[str]
    domains: tuple[str, ...]
    tools: tuple[str, ...]
    max_files: int
    max_bytes: int
    timeout_seconds: int
    max_attempts: int = 5


# This is intentionally a short allowlist, not a downloader registry.  A
# source has to opt in here *and* call this module from its own worker.
_CAPABILITIES: dict[str, FallbackCapability] = {
    "instagram": FallbackCapability("instagram", "instagram", frozenset({"post"}),
                                    ("instagram.com",), ("gallery-dl", "yt-dlp"), 10, 256 * 1024 * 1024, 300),
    "pinterest": FallbackCapability("pinterest", "pinterest", frozenset({"profile", "post"}),
                                    ("pinterest.com",), ("gallery-dl", "yt-dlp"), 10, 256 * 1024 * 1024, 300),
    # Search owns only an already-approved stable embedded URL; it may not
    # turn arbitrary search results into a durable downloader queue.
    "search": FallbackCapability("search", "search", frozenset({"embed"}),
                                  (), ("yt-dlp",), 10, 256 * 1024 * 1024, 300),
    # Airbnb deliberately accepts listing pages, never host profiles or media
    # assets.  Listing extraction remains owned by the Airbnb profile worker.
    "airbnb": FallbackCapability("airbnb", "airbnb", frozenset({"post"}),
                                   ("airbnb.com", "airbnb.com.sg"), ("yt-dlp",), 10, 256 * 1024 * 1024, 300),
}
# Cookie materialization is intentionally separate from fallback admission.
# TikTok and YouTube have source-owned downloaders but are not generic fallback
# queue capabilities.
_COOKIE_VAULT_DOMAINS: dict[str, tuple[str, ...]] = {
    **{source: policy.domains for source, policy in _CAPABILITIES.items()},
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com",),
    "strava": ("strava.com",),
    "snapchat": ("snapchat.com",),
}
_COOKIE_VAULT_AUTH_MARKERS: dict[str, frozenset[str]] = {
    "instagram": frozenset({"sessionid"}),
    "pinterest": frozenset({"_pinterest_sess"}),
    "airbnb": frozenset({"_aat"}),
    "tiktok": frozenset({"sessionid", "ttwid"}),
    "youtube": frozenset({"SID"}),
    "strava": frozenset({"_strava4_session"}),
    # Snapchat changes the session-cookie name between web releases. A
    # first-party cookie is still enough to construct the authenticated client.
    "snapchat": frozenset(),
}
_GALLERY_SOURCES = frozenset(source for source, policy in _CAPABILITIES.items() if "gallery-dl" in policy.tools)
_YTDLP_SOURCES = frozenset(source for source, policy in _CAPABILITIES.items() if "yt-dlp" in policy.tools)
_MEDIA_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mkv", ".mp3", ".m4a"})
_VOLATILE_MARKERS = ("signature", "expires", "expiration", "x-amz-", "token", "policy", "key-pair-id")


def fallback_capability(source: str) -> FallbackCapability | None:
    return _CAPABILITIES.get(str(source or "").strip().lower())


def _capability_reason(source: str, kind: str) -> str:
    policy = fallback_capability(source)
    if policy is None:
        return "private_attachment_native_only" if str(source).lower() in {"telegram", "whatsapp", "beeper"} else "unsupported"
    return "unsupported" if str(kind).lower() not in policy.kinds else "provider_retry"


def canonical_fallback_url(source: str, kind: str, url: str) -> str | None:
    """Return a first-party, stable URL or ``None``.

    CDN and signed asset URLs are intentionally rejected: they leak ephemeral
    credentials into a durable queue and are not a safe retry target.
    """
    source = str(source or "").strip().lower()
    kind = str(kind or "").strip().lower()
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme != "https" or not host or not parsed.path:
        return None
    if any(marker in host or marker in parsed.query.lower() for marker in _VOLATILE_MARKERS):
        return None
    policy = fallback_capability(source)
    if policy is None or kind not in policy.kinds:
        return None
    if policy.domains and not any(host == root or host.endswith("." + root) for root in policy.domains):
        return None
    if source == "airbnb" and not re.fullmatch(r"/rooms/[0-9]+/?", parsed.path):
        return None
    # Query values can contain access tokens even when their key is innocuous;
    # no fallback capability needs them, so never persist any query string.
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", ""))


async def enqueue_extractor_fallback(pool, *, source: str, kind: str, content_id: str, url: str) -> bool:
    target_url = canonical_fallback_url(source, kind, url)
    content_id = str(content_id or "").strip()
    if not target_url or not content_id:
        return False
    async with pool.acquire() as conn:
        status = await conn.execute(
            """INSERT INTO extractor_fallback_queue (source, kind, content_id, target_url)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (source, kind, content_id) DO NOTHING""",
            source.lower(), kind.lower(), content_id[:255], target_url,
        )
    return status.endswith(" 1")


async def handoff_browser_fallback(pool, *, source: str, kind: str, content_id: str,
                                   url: str, exhausted: bool) -> bool:
    """Queue a browser-owned item only after the browser retry budget ended."""
    return bool(exhausted) and await enqueue_extractor_fallback(
        pool, source=source, kind=kind, content_id=content_id, url=url,
    )


async def claim_extractor_fallback_jobs(pool, source: str, limit: int = 5) -> list[Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await conn.fetch(
                """WITH claimed AS (
                     SELECT id FROM extractor_fallback_queue
                      WHERE source=$1 AND (
                           (status IN ('pending', 'retry') AND next_retry_at <= now())
                           OR (status='processing' AND claimed_at < now() - interval '15 minutes')
                      )
                      ORDER BY next_retry_at, id
                      FOR UPDATE SKIP LOCKED LIMIT $2
                   )
                   UPDATE extractor_fallback_queue q
                      SET status='processing', attempts=q.attempts+1, claimed_at=now()
                     FROM claimed WHERE q.id=claimed.id
                   RETURNING q.*""",
                source.lower(), max(1, min(int(limit), 25)),
            )


async def _finish(pool, job_id: int, *, status: str, result: dict[str, Any], delay_seconds: int | None = None) -> None:
    async with pool.acquire() as conn:
        if delay_seconds is None:
            await conn.execute(
                "UPDATE extractor_fallback_queue SET status=$2, result=$3::jsonb, finished_at=now() WHERE id=$1",
                job_id, status, json.dumps(result),
            )
        else:
            await conn.execute(
                """UPDATE extractor_fallback_queue
                      SET status='retry', result=$2::jsonb,
                          next_retry_at=now()+($3 * interval '1 second'), claimed_at=NULL
                    WHERE id=$1""",
                job_id, json.dumps(result), delay_seconds,
            )


def _source_cookie_roots(source: str) -> tuple[str, ...]:
    return _COOKIE_VAULT_DOMAINS.get(source.lower(), ())


def _snapshot_cookies(vault_dir: Path, source: str) -> list[dict[str, Any]]:
    required_markers = _COOKIE_VAULT_AUTH_MARKERS.get(source.lower(), frozenset())
    paths = sorted(vault_dir.glob("snapshot_*.json"), reverse=True) + [vault_dir / "latest.json"]
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cookies = payload if isinstance(payload, list) else payload.get("cookies", [])
            if isinstance(cookies, list):
                rows = [cookie for cookie in cookies if isinstance(cookie, dict)]
                filtered = filter_vault_cookies(source, rows)
                if filtered and (not required_markers or required_markers.issubset(
                    {str(cookie.get("name") or "") for cookie in filtered}
                )):
                    return rows
        except (OSError, ValueError, TypeError):
            continue
    return []


def filter_vault_cookies(source: str, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roots = _source_cookie_roots(source)
    filtered: list[dict[str, Any]] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if (str(cookie.get("name") or "") and str(cookie.get("value") or "")
                and any(domain == root or domain.endswith("." + root) for root in roots)):
            filtered.append(cookie)
    return filtered


def netscape_cookie_jar(cookies: list[dict[str, Any]]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if not domain:
            continue
        try:
            expires = max(0, int(float(cookie.get("expires") or 0)))
        except (TypeError, ValueError, OverflowError):
            expires = 0
        lines.append("\t".join((domain, "TRUE" if domain.startswith(".") else "FALSE",
                                 str(cookie.get("path") or "/"),
                                 "TRUE" if cookie.get("secure", True) else "FALSE", str(expires),
                                 str(cookie.get("name")), str(cookie.get("value")))))
    return "\n".join(lines) + "\n"


def materialize_vault_cookie_jar(source: str, destination: str | Path,
                                 vault_dir: str | Path | None = None) -> str | None:
    """Write a 0600 source-filtered jar into a caller-owned temporary directory."""
    root = Path(vault_dir or os.getenv("BROWSER_COOKIE_VAULT_DIR", "credentials/browser_cookies"))
    cookies = filter_vault_cookies(source, _snapshot_cookies(root, source))
    if not cookies:
        return None
    path = Path(destination)
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            handle.write(netscape_cookie_jar(cookies))
    except OSError:
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    logger.debug("extractor fallback prepared %d %s cookies", len(cookies), source)
    return str(path)


@contextmanager
def temporary_vault_cookie_jar(source: str, vault_dir: str | Path | None = None) -> Iterator[str | None]:
    """Materialize a source-filtered 0600 jar and remove it after subprocess use."""
    fd, name = tempfile.mkstemp(prefix=f"uc-{source}-", suffix=".cookies")
    os.close(fd)
    try:
        yield materialize_vault_cookie_jar(source, name, vault_dir)
    finally:
        with contextlib.suppress(OSError):
            Path(name).unlink()


def _media_files(files: list[Path]) -> list[Path]:
    return [path for path in files if path.is_file() and path.suffix.lower() in _MEDIA_SUFFIXES]


def _file_has_expected_magic(path: Path) -> bool:
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return False
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    if suffix in {".mp4", ".m4a"}:
        return len(head) >= 8 and head[4:8] == b"ftyp"
    if suffix in {".webm", ".mkv"}:
        return head.startswith(b"\x1aE\xdf\xa3")
    if suffix == ".mp3":
        return head.startswith(b"ID3") or head.startswith(b"\xff\xfb")
    return False


def validated_media_files(files: list[Path], policy: FallbackCapability) -> list[Path]:
    """Enforce the capability's file count, byte and extension/magic contract."""
    accepted: list[Path] = []
    total = 0
    for path in _media_files(files):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not 0 < size <= policy.max_bytes or total + size > policy.max_bytes:
            continue
        if not _file_has_expected_magic(path):
            continue
        accepted.append(path)
        total += size
        if len(accepted) >= policy.max_files:
            break
    return accepted


async def _ingest(pool, job, files: list[Path], tool: str) -> int:
    stored = 0
    for index, path in enumerate(files):
        artifact_id = str(job["content_id"]) if index == 0 else f"{job['content_id']}:{index}"
        artifact = write_atomic_artifact_from_path(
            source=job["source"], artifact_id=artifact_id, artifact_kind="extractor_fallback",
            source_path=path, extension=path.suffix.lstrip(".") or None,
            metadata={"source_url": job["target_url"], "extractor": tool, "queue_id": job["id"]},
            root=VAULT_ROOT, delete_source=False,
        )
        if not artifact.ok or artifact.path is None:
            raise RuntimeError(artifact.error or "vault artifact write failed")
        if not artifact.path.is_file() or artifact.path.stat().st_size != artifact.file_size:
            raise RuntimeError("vault artifact readback failed")
        async with pool.acquire() as conn:
            inserted = await conn.execute(
                """INSERT INTO media_items (source, entity_id, entity_name, content_type, content_id,
                                              filename, file_path, file_size, sha256, source_url, metadata, ingest_path, kind)
                   VALUES ($1,$2,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,'extractor','post')
                   ON CONFLICT DO NOTHING""",
                job["source"], str(job["content_id"]), "video" if path.suffix.lower() in {".mp4", ".webm", ".mkv"} else "image",
                artifact_id, path.name[:255], str(artifact.path), artifact.file_size, artifact.sha256,
                job["target_url"], json.dumps({"extractor": tool, "queue_id": job["id"], "vault_artifact": {"sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None}}),
            )
            # A duplicate is still successful only when the canonical media
            # ledger already points at the verified artifact.
            verified = await conn.fetchval(
                "SELECT 1 FROM media_items WHERE source=$1 AND content_id=$2 AND sha256=$3 AND file_path=$4 LIMIT 1",
                job["source"], artifact_id, artifact.sha256, str(artifact.path),
            )
            if verified != 1:
                raise RuntimeError("media ledger readback failed")
            stored += int(inserted.endswith(" 1"))
    return stored


async def run_extractor_fallback(pool, source: str, limit: int | None = None) -> dict[str, int | str]:
    """Drain a bounded source-local batch. Never claims another source's rows."""
    source = source.lower()
    policy = fallback_capability(source)
    summary: dict[str, int | str] = {"claimed": 0, "stored": 0, "done": 0, "retry": 0, "terminal": 0}
    if policy is None:
        summary["reason"] = _capability_reason(source, "")
        return summary
    from src.core.source_controls import automatic_cooldown_seconds, is_manually_paused
    if await is_manually_paused(pool, source) or await automatic_cooldown_seconds(pool, source):
        summary["reason"] = "policy_halted"
        return summary
    for job in await claim_extractor_fallback_jobs(pool, source, min(policy.max_files, limit or int(os.getenv("EXTRACTOR_FALLBACK_BATCH", "5")))):
        summary["claimed"] += 1
        try:
            async with managed_tempdir(f"extractor_{source}_") as tmpdir:
                with temporary_vault_cookie_jar(source) as cookie_file:
                    result = None
                    tool = "yt-dlp"
                    gallery_error = ""
                    if "gallery-dl" in policy.tools:
                        tool = "gallery-dl"
                        try:
                            result = await gallery_dl_download(job["target_url"], cookies_file=cookie_file, timeout=300, tempdir=tmpdir)
                        except Exception as exc:
                            gallery_error = type(exc).__name__
                    files = validated_media_files(result.files if result else [], policy)
                    if not files and "yt-dlp" in policy.tools:
                        tool = "yt-dlp"
                        result = await yt_dlp_download(job["target_url"], cookies_file=cookie_file, max_downloads=policy.max_files, timeout=policy.timeout_seconds, tempdir=tmpdir)
                        files = validated_media_files(result.files, policy)
                    if files:
                        summary["stored"] += await _ingest(pool, job, files, tool)
                        await _finish(pool, job["id"], status="done", result={"tool": tool, "files": len(files)})
                        summary["done"] += 1
                        continue
                    detail = (result.output_summary(300) if result else gallery_error).lower()
                    if any(word in detail for word in ("login", "sign in", "authentication", "cookie")):
                        reason = "auth_required" if cookie_file else "session_unavailable"
                    elif any(word in detail for word in ("private", "unsupported", "not supported", "unavailable")):
                        reason = "unsupported"
                    else:
                        reason = "provider_retry"
                    if reason != "provider_retry" or int(job["attempts"]) >= policy.max_attempts:
                        await _finish(pool, job["id"], status="terminal", result={"tool": tool, "reason": reason})
                        summary["terminal"] += 1
                    else:
                        await _finish(pool, job["id"], status="retry", result={"tool": tool, "reason": reason}, delay_seconds=min(86400, 300 * 2 ** (int(job["attempts"]) - 1)))
                        summary["retry"] += 1
        except Exception:
            if int(job["attempts"]) >= policy.max_attempts:
                await _finish(pool, job["id"], status="terminal", result={"reason": "provider_retry"})
                summary["terminal"] += 1
            else:
                await _finish(pool, job["id"], status="retry", result={"reason": "provider_retry"}, delay_seconds=min(86400, 300 * 2 ** (int(job["attempts"]) - 1)))
                summary["retry"] += 1
    return summary


async def _main(source: str, *, once: bool = False) -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        while True:
            logger.info("extractor fallback %s: %s", source, await run_extractor_fallback(pool, source, 1 if once else None))
            if once:
                return
            await asyncio.sleep(max(10, int(os.getenv("EXTRACTOR_FALLBACK_INTERVAL_SECONDS", "60"))))
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(_main(args.source, once=args.once))


if __name__ == "__main__":
    main()
