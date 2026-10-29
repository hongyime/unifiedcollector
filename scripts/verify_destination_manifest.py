#!/usr/bin/env python3
"""Fail-closed verifier for a sorted SHA-256 destination manifest."""
from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_HEX = frozenset(b"0123456789abcdef")


@dataclass(frozen=True)
class ManifestReport:
    entries: int
    manifest_sha256: str


def verify_manifest(manifest: Path, allowed_roots: Iterable[Path]) -> ManifestReport:
    """Verify manifest syntax, ordering, root confinement, duplicate paths, and bytes."""
    roots = tuple(root.resolve() for root in allowed_roots)
    if not roots:
        raise ValueError("at least one allowed root is required")
    try:
        raw_manifest = manifest.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read manifest: {exc}") from exc
    lines = raw_manifest.splitlines()
    if not lines:
        raise ValueError("manifest is empty")

    seen: set[Path] = set()
    previous: bytes | None = None
    for line_number, line in enumerate(lines, start=1):
        escaped = line.startswith(b"\\")
        record = line[1:] if escaped else line
        if len(record) < 67 or record[64:66] not in {b"  ", b" *"} or any(byte not in _HEX for byte in record[:64]):
            raise ValueError(f"malformed sha256sum record at line {line_number}")
        digest = record[:64].decode("ascii")
        raw_path = _unescape_filename(record[66:]) if escaped else record[66:]
        if not raw_path:
            raise ValueError(f"empty manifest path at line {line_number}")
        path = Path(os.fsdecode(raw_path))
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"manifest path missing at line {line_number}: {os.fsdecode(raw_path)}") from exc
        if not any(resolved.is_relative_to(root) for root in roots):
            raise ValueError(f"manifest path outside allowed roots at line {line_number}: {os.fsdecode(raw_path)}")
        canonical = os.fsencode(resolved)
        if previous is not None and canonical <= previous:
            raise ValueError(f"manifest paths are not strictly sorted at line {line_number}")
        if resolved in seen:
            raise ValueError(f"duplicate manifest path at line {line_number}: {os.fsdecode(raw_path)}")
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(f"sha256 mismatch at line {line_number}: {os.fsdecode(raw_path)}")
        seen.add(resolved)
        previous = canonical

    return ManifestReport(entries=len(seen), manifest_sha256=hashlib.sha256(raw_manifest).hexdigest())


def _unescape_filename(value: bytes) -> bytes:
    """Decode GNU coreutils' backslash/newline/carriage-return filename escapes."""
    result = bytearray()
    index = 0
    escape_map = {ord("\\"): ord("\\"), ord("n"): ord("\n"), ord("r"): ord("\r")}
    while index < len(value):
        byte = value[index]
        if byte != ord("\\"):
            result.append(byte)
            index += 1
            continue
        if index + 1 == len(value) or value[index + 1] not in escape_map:
            raise ValueError("invalid GNU sha256sum filename escape")
        result.append(escape_map[value[index + 1]])
        index += 2
    return bytes(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        report = verify_manifest(args.manifest, args.root)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"PASS: entries={report.entries} manifest_sha256={report.manifest_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
