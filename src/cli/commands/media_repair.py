"""Media repair / recovery / audit commands.

Covers ``repair-media-sidecars``, ``repair-media-file-paths``,
``recover-missing-media-files``, ``media-artifact-audit``.
"""
from __future__ import annotations

import json

from src.db.connection import close_pool, get_pool


# ── repair-media-sidecars ─────────────────────────────────────

async def _cmd_repair_media_sidecars(args):
    from src.core.media_sidecar_repair import (
        repair_missing_media_sidecars,
        repair_partial_vault_artifacts,
    )

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            if args.partial_artifacts:
                report = await repair_partial_vault_artifacts(
                    conn,
                    source=args.source,
                    limit=args.limit,
                    cursor_after=args.cursor_after,
                    timeout=args.timeout,
                    vault_root=args.vault_root,
                    dry_run=args.dry_run,
                )
            else:
                report = await repair_missing_media_sidecars(
                    conn,
                    source=args.source,
                    limit=args.limit,
                    since_hours=args.since_hours,
                    cursor_after=args.cursor_after,
                    timeout=args.timeout,
                    vault_root=args.vault_root,
                    dry_run=args.dry_run,
                )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(
                "Media sidecar repair: "
                f"scanned={report.scanned} repaired={report.repaired} "
                f"failed={report.failed} skipped={report.skipped} "
                f"would_repair={report.would_repair} already_ok={report.already_ok} "
                f"file_missing={report.file_missing} size_mismatch={report.size_mismatch} "
                f"next_cursor={report.next_cursor}"
            )
            for failure in report.failures[:10]:
                print(f"  failed {failure.get('source')}/{failure.get('content_id')}: {failure.get('error')}")
    finally:
        await close_pool()


# ── media-artifact-audit ──────────────────────────────────────

async def _cmd_media_artifact_audit(args):
    from src.core.media_artifact_audit import audit_media_artifacts

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            report = await audit_media_artifacts(
                conn,
                source=args.source,
                sample_per_source=args.sample_per_source,
                cursor_after=args.cursor_after,
                timeout=args.timeout,
                vault_root=args.vault_root,
            )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(
                "Media artifact audit: "
                f"mode={report.mode} sampled={report.total_sampled} "
                f"issues={report.total_issues} vault={report.vault_root}"
            )
            if report.source_error:
                print(f"  source listing failed: {report.source_error}")
            for source_report in report.sources:
                parts = [
                    f"{source_report.source}: sampled={source_report.sampled}",
                    f"total={source_report.total_media_items}",
                    f"issues={source_report.issue_count}",
                    f"file_missing={source_report.files_missing}",
                    f"size_mismatch={source_report.size_mismatches}",
                    f"sidecar_meta_missing={source_report.sidecar_metadata_missing}",
                    f"sidecar_file_missing={source_report.sidecar_files_missing}",
                    f"next_cursor={source_report.next_cursor}",
                ]
                if source_report.query_error:
                    parts.append(f"query_error={source_report.query_error}")
                print("  " + " ".join(parts))
                for failure in source_report.failures[:5]:
                    print(
                        "    "
                        f"{failure.get('kind')} {failure.get('content_id')}: "
                        f"{failure.get('detail') or failure.get('path')}"
                    )
    finally:
        await close_pool()


# ── repair-media-file-paths ───────────────────────────────────

async def _cmd_repair_media_file_paths(args):
    from src.core.media_sidecar_repair import repair_media_file_paths_from_blobs

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            report = await repair_media_file_paths_from_blobs(
                conn,
                source=args.source,
                limit=args.limit,
                cursor_after=args.cursor_after,
                timeout=args.timeout,
                vault_root=args.vault_root,
                dry_run=args.dry_run,
            )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(
                "Media file path repair: "
                f"source={args.source} scanned={report.scanned} repaired={report.repaired} "
                f"failed={report.failed} skipped={report.skipped} "
                f"would_repair={report.would_repair} already_ok={report.already_ok} "
                f"file_missing={report.file_missing} size_mismatch={report.size_mismatch} "
                f"next_cursor={report.next_cursor}"
            )
            for failure in report.failures[:10]:
                print(f"  failed {failure.get('source')}/{failure.get('content_id')}: {failure.get('error')}")
    finally:
        await close_pool()


# ── recover-missing-media-files ───────────────────────────────

async def _cmd_recover_missing_media_files(args):
    from src.core.media_sidecar_repair import recover_missing_media_files

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            report = await recover_missing_media_files(
                conn,
                source=args.source,
                limit=args.limit,
                cursor_after=args.cursor_after,
                timeout=args.timeout,
                vault_root=args.vault_root,
                dry_run=args.dry_run,
                max_bytes=args.max_bytes,
                request_timeout=args.request_timeout,
                delay_seconds=args.delay,
                queue_platform_backfill=args.queue_platform_backfill,
            )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        else:
            print(
                "Missing media recovery: "
                f"source={args.source or 'all'} scanned={report.scanned} repaired={report.repaired} "
                f"redownloaded={report.redownloaded} failed={report.failed} skipped={report.skipped} "
                f"would_repair={report.would_repair} already_ok={report.already_ok} "
                f"file_missing={report.file_missing} size_mismatch={report.size_mismatch} "
                f"canonical_blob_available={report.canonical_blob_available} "
                f"unsafe_response={report.unsafe_response} "
                f"platform_backfill_required={report.platform_backfill_required} "
                f"no_direct_url={report.no_direct_url} queued_backfill={report.queued_backfill} "
                f"target_enqueued={report.target_enqueued} "
                f"would_enqueue_target={report.would_enqueue_target} "
                f"next_cursor={report.next_cursor}"
            )
            for source_name, stats in sorted(report.sources.items()):
                parts = [f"{key}={value}" for key, value in sorted(stats.items())]
                print(f"  {source_name}: " + " ".join(parts))
            for failure in report.failures[:10]:
                print(
                    "  "
                    f"{failure.get('source')}/{failure.get('content_id')}: "
                    f"{failure.get('action') or 'skipped'}: {failure.get('error')}"
                )
    finally:
        await close_pool()


# ── argparse registration ─────────────────────────────────────

def register(subparsers) -> None:
    msr = subparsers.add_parser(
        "repair-media-sidecars",
        help="Repair media_items rows that have files but lack occurrence sidecar metadata",
    )
    msr.add_argument("--source", default=None, help="Optional source filter")
    msr.add_argument("--limit", type=int, default=500, help="Maximum rows to scan")
    msr.add_argument("--since-hours", type=int, default=None, help="Only inspect rows collected in this window")
    msr.add_argument("--cursor-after", default="", help="Start after this content_id when --source is set")
    msr.add_argument("--timeout", type=float, default=10.0, help="DB timeout for the repair scan in seconds")
    msr.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    msr.add_argument("--dry-run", action="store_true", help="Report repairable rows without writing sidecars")
    msr.add_argument(
        "--partial-artifacts",
        action="store_true",
        help="Repair media rows whose canonical vault artifact sidecar previously failed",
    )
    msr.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    mfpr = subparsers.add_parser(
        "repair-media-file-paths",
        help="Repair missing/stale media file_path rows by pointing them at existing sha256 vault blobs",
    )
    mfpr.add_argument("--source", required=True, help="Source to repair")
    mfpr.add_argument("--limit", type=int, default=100, help="Maximum rows to scan")
    mfpr.add_argument("--cursor-after", default="", help="Start after this content_id for keyset paging")
    mfpr.add_argument("--timeout", type=float, default=10.0, help="DB timeout for the repair scan in seconds")
    mfpr.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    mfpr.add_argument("--dry-run", action="store_true", help="Report repairable rows without updating DB")
    mfpr.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    # Deferred import: MISSING_MEDIA_RECOVERY_SOURCES is defined in the same
    # module the handler imports lazily. Import it here at parser-build time
    # (before dispatch) so ``--source`` choices remain identical.
    from src.core.media_sidecar_repair import MISSING_MEDIA_RECOVERY_SOURCES

    mmr = subparsers.add_parser(
        "recover-missing-media-files",
        help="Safely recover missing media files from source-specific direct URLs",
    )
    mmr.add_argument("--source", choices=MISSING_MEDIA_RECOVERY_SOURCES, default=None, help="Optional source filter")
    mmr.add_argument("--limit", type=int, default=25, help="Maximum rows to scan")
    mmr.add_argument("--cursor-after", default="", help="Start after this content_id when --source is set")
    mmr.add_argument("--timeout", type=float, default=10.0, help="DB timeout for the recovery scan in seconds")
    mmr.add_argument("--request-timeout", type=float, default=20.0, help="HTTP timeout per direct media request")
    mmr.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024, help="Maximum bytes per recovered media file")
    mmr.add_argument("--delay", type=float, default=0.25, help="Delay between network recovery attempts")
    mmr.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    mmr.add_argument("--dry-run", action="store_true", help="Report candidate rows without network writes")
    mmr.add_argument(
        "--queue-platform-backfill",
        action="store_true",
        help="Queue report-only platform rows in dead_letter_queue",
    )
    mmr.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    maa = subparsers.add_parser(
        "media-artifact-audit",
        help="Read-only bounded audit of DB media rows, local files, and sidecar files",
    )
    maa.add_argument("--source", default=None, help="Optional source filter")
    maa.add_argument("--sample-per-source", type=int, default=100, help="Rows to sample per source")
    maa.add_argument("--cursor-after", default="", help="Start after this content_id for keyset paging")
    maa.add_argument("--timeout", type=float, default=5.0, help="DB timeout per source query in seconds")
    maa.add_argument("--vault-root", default=None, help="Vault root (default: COLLECTOR_VAULT_ROOT)")
    maa.add_argument("--json", action="store_true", help="Print machine-readable JSON")


HANDLERS = {
    "repair-media-sidecars": _cmd_repair_media_sidecars,
    "repair-media-file-paths": _cmd_repair_media_file_paths,
    "recover-missing-media-files": _cmd_recover_missing_media_files,
    "media-artifact-audit": _cmd_media_artifact_audit,
}
