"""Rebuild + vault-inspect commands.

Covers ``rebuild-report``, ``rebuild-rehearsal``, ``vault-inspect``.

The ``_cmd_rebuild_report`` and ``_attach_rebuild_report_db_comparison``
callables keep their historical positional signatures — they are directly
invoked (and monkey-patched) by ``tests/core/test_rebuild_report.py``.
"""
from __future__ import annotations

import asyncio
import json

from src.db.connection import close_pool, get_pool


# ── rebuild-report ────────────────────────────────────────────

async def _cmd_rebuild_report(
    vault_root: str | None,
    as_json: bool,
    verify_checksums: bool = False,
    compare_db: bool = False,
    compare_db_limit: int | None = None,
    sidecar_limit: int | None = None,
    blob_limit: int | None = None,
):
    from src.core.rebuild_report import (
        db_compare_timeout_seconds,
        scan_sidecars,
    )

    report = scan_sidecars(vault_root, verify_checksums=verify_checksums, sidecar_limit=sidecar_limit)
    if compare_db:
        timeout = db_compare_timeout_seconds()
        try:
            await asyncio.wait_for(
                _attach_rebuild_report_db_comparison(
                    report,
                    vault_root,
                    verify_checksums,
                    compare_db_limit,
                    sidecar_limit,
                    blob_limit,
                    timeout,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            report.db_comparison_enabled = True
            report.db_compare_error = "compare_db_timeout"
            report.db_compare_timeout_seconds = timeout
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(report.to_text())


async def _attach_rebuild_report_db_comparison(
    report,
    vault_root: str | None,
    verify_checksums: bool,
    compare_db_limit: int | None,
    sidecar_limit: int | None,
    blob_limit: int | None,
    timeout: float,
):
    from src.core.rebuild_report import compare_db_media_artifacts

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await compare_db_media_artifacts(
                report,
                conn,
                vault_root,
                verify_checksums=verify_checksums,
                limit=compare_db_limit,
                sidecar_limit=sidecar_limit,
                blob_limit=blob_limit,
                db_fetch_timeout=timeout,
            )
    finally:
        await close_pool()


# ── rebuild-rehearsal ─────────────────────────────────────────

def _cmd_rebuild_rehearsal(
    vault_root: str | None,
    scratch_db: str | None,
    sidecar_limit: int | None,
    raw_payload_limit: int | None,
    verify_files: bool,
    as_json: bool,
):
    from src.core.rebuild_rehearsal import rehearse_media_items_rebuild

    report = rehearse_media_items_rebuild(
        vault_root,
        scratch_db=scratch_db,
        sidecar_limit=sidecar_limit,
        raw_payload_limit=raw_payload_limit,
        verify_files=verify_files,
    )
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(report.to_text())


# ── vault-inspect ─────────────────────────────────────────────

def _cmd_vault_inspect(
    vault_root: str | None,
    source: str | None,
    limit: int,
    as_json: bool,
):
    from src.core.vault_inspect import inspect_vault

    report = inspect_vault(vault_root, source=source, limit=limit)
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(report.to_text())


# ── argparse registration + dispatch wrappers ─────────────────

def _handle_rebuild_report(args):
    return _cmd_rebuild_report(
        args.vault_root,
        args.json,
        args.verify_checksums,
        args.compare_db,
        args.compare_db_limit,
        args.sidecar_limit,
        args.blob_limit,
    )


def _handle_rebuild_rehearsal(args):
    _cmd_rebuild_rehearsal(
        args.vault_root,
        args.scratch_db,
        args.sidecar_limit,
        args.raw_payload_limit,
        not args.no_verify_files,
        args.json,
    )


def _handle_vault_inspect(args):
    _cmd_vault_inspect(args.vault_root, args.source, args.limit, args.json)


def register(subparsers) -> None:
    rp = subparsers.add_parser("rebuild-report", help="Dry-run rebuild coverage from vault sidecars")
    rp.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    rp.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    rp.add_argument("--verify-checksums", action="store_true", help="Hash referenced files while scanning")
    rp.add_argument("--compare-db", action="store_true", help="Also compare vault artifacts against media_items")
    rp.add_argument("--compare-db-limit", type=int, default=None, help="Limit media_items rows for a quick sample")
    rp.add_argument("--sidecar-limit", type=int, default=None, help="Limit sidecars scanned for a quick sample")
    rp.add_argument("--blob-limit", type=int, default=None, help="Limit canonical blob files scanned for a quick sample")

    rr = subparsers.add_parser(
        "rebuild-rehearsal",
        help="Materialize media and raw-payload sidecars into a scratch SQLite DB",
    )
    rr.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    rr.add_argument("--scratch-db", default=None, help="Scratch SQLite path (default: in-memory)")
    rr.add_argument("--sidecar-limit", type=int, default=None, help="Limit media sidecars scanned for a quick sample")
    rr.add_argument("--raw-payload-limit", type=int, default=None, help="Limit raw-payload sidecars scanned (defaults to --sidecar-limit)")
    rr.add_argument("--no-verify-files", action="store_true", help="Skip file existence/size checks")
    rr.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    vi = subparsers.add_parser(
        "vault-inspect",
        help="Inspect vault sidecars and file/raw references without DB access",
    )
    vi.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    vi.add_argument("--source", default=None, help="Optional source filter")
    vi.add_argument("--limit", type=int, default=20, help="Maximum artifacts to return")
    vi.add_argument("--json", action="store_true", help="Print machine-readable JSON")


HANDLERS = {
    "rebuild-report": _handle_rebuild_report,
    "rebuild-rehearsal": _handle_rebuild_rehearsal,
    "vault-inspect": _handle_vault_inspect,
}
