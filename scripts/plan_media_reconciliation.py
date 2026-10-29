#!/usr/bin/env python3
"""Create a read-only, hash-bound Branch-B media reconciliation plan."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.media_reconciliation import build_reconciliation_plan, parse_sha256_manifest, read_rows_csv, write_plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows-csv", type=Path, required=True, help="private CSV export of id,file_path,file_size,sha256")
    parser.add_argument("--destination-manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    plan = build_reconciliation_plan(
        read_rows_csv(args.rows_csv), parse_sha256_manifest(args.destination_manifest), args.root
    )
    write_plan(plan, args.output, rows_csv=args.rows_csv, manifest=args.destination_manifest, root=args.root)
    print("PASS:", " ".join(f"{key}={value}" for key, value in plan.summary().items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
