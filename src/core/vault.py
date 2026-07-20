"""Collector vault helpers.

The collector DB is the live index, but the Z: vault is the durable evidence
store. This module centralizes path handling and per-artifact sidecar writes so
collectors do not each invent their own recovery metadata format.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SIDECARS_ENABLED = _env_bool("COLLECTOR_SIDECARS_ENABLED", True)


def _default_vault_root() -> Path:
    configured = os.getenv("COLLECTOR_VAULT_ROOT")
    if configured:
        return Path(configured).resolve()
    container_root = Path("/vault")
    if container_root.exists() and container_root.is_dir():
        return container_root.resolve()
    drive_path = Path(os.getenv("COLLECTOR_DRIVE_PATH", "Z:/unifiedcollector/media"))
    # Host default is Z:/unifiedcollector/media, so sidecars belong one level up.
    # Container default is /media, where only that directory is mounted; keep
    # sidecars under /media until the compose root-vault mount lands everywhere.
    if drive_path.as_posix() not in {"/media", "media"} and drive_path.name.lower() == "media":
        return drive_path.parent.resolve()
    return drive_path.resolve()


VAULT_ROOT = _default_vault_root()


@dataclass(frozen=True)
class VaultHealth:
    root: Path
    available: bool
    writable: bool
    error: str | None = None


@dataclass(frozen=True)
class SidecarResult:
    enabled: bool
    ok: bool
    path: Path | None = None
    relative_path: str | None = None
    error: str | None = None


def vault_health(root: Path = VAULT_ROOT) -> VaultHealth:
    """Return mount/writability health for the canonical vault root."""
    if not root.exists() or not root.is_dir():
        return VaultHealth(root=root, available=False, writable=False, error="vault root missing")
    probe = root / f".vault_check.{os.getpid()}.{time.time_ns()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return VaultHealth(root=root, available=True, writable=True)
    except OSError as exc:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return VaultHealth(root=root, available=True, writable=False, error=str(exc))


def ensure_vault_available(root: Path = VAULT_ROOT) -> None:
    health = vault_health(root)
    if not health.available or not health.writable:
        raise RuntimeError(f"collector vault unavailable: {health.error or root}")


def relative_to_vault(path: str | os.PathLike[str] | None, root: Path = VAULT_ROOT) -> str | None:
    """Best-effort vault-relative path for stable DB/sidecar references."""
    if not path:
        return None
    try:
        p = Path(path).resolve()
        return p.relative_to(root).as_posix()
    except Exception:
        return str(path)


_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9_.=-]+")


def _safe_part(value: Any, *, fallback: str = "unknown", limit: int = 96) -> str:
    text = str(value or fallback).strip() or fallback
    text = _SAFE_PART_RE.sub("_", text)
    return text[:limit].strip("._") or fallback


def sidecar_path_for_media(
    *,
    source: str,
    content_id: str,
    collected_at: datetime | None = None,
    root: Path = VAULT_ROOT,
) -> Path:
    ts = collected_at or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (
        root
        / "sidecars"
        / _safe_part(source)
        / f"{ts:%Y}"
        / f"{ts:%m}"
        / f"{_safe_part(content_id, limit=140)}.json"
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, sort_keys=True, indent=2, default=str)
            f.write("\n")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def write_media_sidecar(
    *,
    source: str,
    entity_id: str,
    entity_name: str,
    content_type: str,
    content_id: str,
    filename: str,
    file_path: str,
    file_size: int | None,
    width: int | None,
    height: int | None,
    sha256: str | None,
    source_url: str | None,
    metadata: dict | None,
    ingest_path: str | None,
    kind: str | None,
    root: Path = VAULT_ROOT,
) -> SidecarResult:
    """Write one JSON sidecar for a stored media artifact.

    Sidecars are intentionally source-occurrence records, not physical blob
    dedupe records. If the same sha256 appears through three sources, all three
    occurrences can have their own sidecar while sharing one physical blob.
    """
    if not SIDECARS_ENABLED:
        return SidecarResult(enabled=False, ok=True)
    try:
        ensure_vault_available(root)
        now = datetime.now(timezone.utc)
        sidecar_path = sidecar_path_for_media(source=source, content_id=content_id, collected_at=now, root=root)
        payload = {
            "schema_version": 1,
            "artifact_kind": "media",
            "artifact_id": f"{source}:{content_id}",
            "source": source,
            "ingest_path": ingest_path,
            "collection_priority": (metadata or {}).get("collection_priority"),
            "entity": {
                "id": entity_id,
                "name": entity_name,
            },
            "content": {
                "type": content_type,
                "kind": kind or "post",
                "id": content_id,
                "filename": filename,
                "source_url": source_url,
                "caption": (metadata or {}).get("caption"),
                "text": (metadata or {}).get("text"),
            },
            "file": {
                "path": relative_to_vault(file_path, root),
                "absolute_path": str(file_path) if file_path else None,
                "size": file_size,
                "width": width,
                "height": height,
                "sha256": sha256,
            },
            "timestamps": {
                "collected_at": now.isoformat(),
                "posted_at": (metadata or {}).get("posted_at") or (metadata or {}).get("timestamp"),
                "discovered_at": (metadata or {}).get("discovered_at"),
            },
            "raw_payload": {
                "inline": (metadata or {}).get("raw"),
                "path": (metadata or {}).get("raw_payload_path"),
            },
            "provenance": {
                "platform_ids": (metadata or {}).get("platform_ids"),
                "collection_account": (metadata or {}).get("collection_account"),
                "scrape_run_id": (metadata or {}).get("scrape_run_id"),
                "extension_version": (metadata or {}).get("extension_version"),
                "request_url": (metadata or {}).get("request_url"),
                "http_status": (metadata or {}).get("http_status"),
                "rate_limit_scope": (metadata or {}).get("rate_limit_scope"),
                "partial": False,
            },
            "metadata": metadata or {},
        }
        _atomic_write_json(sidecar_path, payload)
        return SidecarResult(
            enabled=True,
            ok=True,
            path=sidecar_path,
            relative_path=relative_to_vault(sidecar_path, root),
        )
    except Exception as exc:
        return SidecarResult(enabled=True, ok=False, error=str(exc))
