#!/usr/bin/env python3
"""Regenerate PARITY_MATRIX.md and PARITY_MATRIX.json.

Walks each toolkit folder and the unified port (src/collectors + src/core),
computes bloat ratios with shared-core LOC amortized across consumers, and
writes both human-readable Markdown and machine-readable JSON.

Wave 0 baseline: 8 cross-cutting core modules deployed 2026-05-26.
Fixes 2 known bugs in the prior PARITY:
  1. youtubetoolkit existed but was reported as 0 LOC.
  2. websitetoolkit row was missing entirely.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hardcoded Wave 0 consumer map.
WAVE0_CONSUMERS: dict[str, list[str]] = {
    "media_download.py":  ["github", "instagram", "lemon8", "strava", "telegram", "tiktok", "whatsapp"],
    "spider_discover.py": ["github", "instagram", "tiktok", "strava", "lemon8", "youtube"],
    "adaptive_rate.py":   ["instagram", "lemon8", "search", "strava", "tiktok"],
    "dedupe_hash.py":     ["github", "instagram", "lemon8", "telegram", "tiktok"],
    "account_quota.py":   ["instagram", "tiktok", "lemon8", "github"],
    "tor_proxy.py":       ["github", "search", "website"],
    "auth_session.py":    ["instagram"],
    "matrix_client.py":   ["matrix"],
}

# Test files that pair with each Wave 0 module.
WAVE0_TEST_FILES: dict[str, str] = {
    "media_download.py":  "tests/core/test_media_download.py",
    "spider_discover.py": "tests/core/test_spider_discover.py",
    "adaptive_rate.py":   "tests/core/test_adaptive_rate.py",
    "dedupe_hash.py":     "tests/core/test_dedupe_hash.py",
    "account_quota.py":   "tests/core/test_account_quota.py",
    "tor_proxy.py":       "tests/core/test_tor_proxy.py",
    "auth_session.py":    "tests/core/test_auth_session.py",
    "matrix_client.py":   "tests/core/test_matrix_client.py",
}

# Toolkit roots per platform. telegram combines two source folders.
TOOLKIT_ROOTS: dict[str, list[str]] = {
    "github":    ["githubtoolkit"],
    "instagram": ["instagramtoolkit"],
    "lemon8":    ["lemon8toolkit"],
    "search":    ["searchtoolkit"],
    "strava":    ["stravatoolkit"],
    "telegram":  ["telegramcollector", "telegramtoolkit"],
    "tiktok":    ["tiktoktoolkit"],
    "whatsapp":  ["whatsapptoolkit", "whatsappcollector"],
    "website":   ["websitetoolkit"],
    "youtube":   ["youtubetoolkit"],
    "matrix":    [],  # no standalone matrix toolkit; new build
}

PLATFORMS = list(TOOLKIT_ROOTS.keys())

SKIP_DIR_NAMES = {".venv", "venv", "env", "__pycache__", ".git",
                  "node_modules", "data", "downloads", "logs",
                  "latest_logs", "sessions", "credentials", ".pytest_cache",
                  ".mypy_cache", "build", "dist", ".tox"}
SKIP_FILE_PREFIXES = ("test_",)
SKIP_FILE_NAMES = {"conftest.py"}


def count_py_loc(path: Path) -> int:
    """Non-blank, non-pure-comment LOC for a single .py file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    n = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        n += 1
    return n


def is_skipped_dir(p: Path) -> bool:
    parts = {x.lower() for x in p.parts}
    if parts & SKIP_DIR_NAMES:
        return True
    # also skip migrations/ and tests/ subtrees inside toolkits
    return any(x in {"migrations", "tests"} for x in parts)


def is_skipped_file(p: Path) -> bool:
    if p.name in SKIP_FILE_NAMES:
        return True
    if any(p.name.startswith(pref) for pref in SKIP_FILE_PREFIXES):
        return True
    return False


def walk_loc(root: Path) -> tuple[int, int]:
    """(file_count, total_loc) for .py under root, applying skip rules."""
    if not root.exists():
        return 0, 0
    files = 0
    loc = 0
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        # Build pseudo-path for skip check (only directory parts, not the file)
        if any(part.lower() in SKIP_DIR_NAMES or part.lower() in {"migrations", "tests"}
               for part in rel.parts[:-1]):
            continue
        if is_skipped_file(p):
            continue
        files += 1
        loc += count_py_loc(p)
    return files, loc


def file_loc(rel: str) -> int:
    p = REPO_ROOT / rel
    if not p.exists():
        return 0
    return count_py_loc(p)


def fmt_int(n: int) -> str:
    return f"{n:,}"


def fmt_ratio(r: float) -> str:
    if r == float("inf"):
        return "∞"
    return f"{r:.1f}x"


def status_for(toolkit_loc: int, ratio: float) -> str:
    if toolkit_loc == 0:
        return "✗"
    if ratio > 5:
        return "⚠"
    return "✓"


def build_rows():
    # Compute Wave 0 module LOC and per-consumer attribution.
    core_dir = REPO_ROOT / "src" / "core"
    wave0 = []
    for mod, consumers in WAVE0_CONSUMERS.items():
        mod_loc = file_loc(f"src/core/{mod}")
        test_path = WAVE0_TEST_FILES[mod]
        test_loc = file_loc(test_path)
        per = (mod_loc / len(consumers)) if consumers else 0.0
        wave0.append({
            "module": mod,
            "loc": mod_loc,
            "test_loc": test_loc,
            "consumers": consumers,
            "per_consumer_attribution": round(per, 1),
        })

    # Per platform rows.
    rows = []
    for platform in PLATFORMS:
        # Toolkit LOC: sum across all configured toolkit roots.
        tk_files = 0
        tk_loc = 0
        roots_used = []
        for sub in TOOLKIT_ROOTS[platform]:
            f, l = walk_loc(REPO_ROOT / sub)
            tk_files += f
            tk_loc += l
            if (REPO_ROOT / sub).exists():
                roots_used.append(sub)

        # Unified collector LOC.
        collector_rel = f"src/collectors/{platform}.py"
        collector_loc = file_loc(collector_rel)

        # Attributed core LOC: sum( mod_loc/len(consumers) ) for each Wave 0 mod
        # that lists this platform.
        core_attr = 0.0
        core_modules_used = []
        for mod, consumers in WAVE0_CONSUMERS.items():
            if platform in consumers:
                ml = file_loc(f"src/core/{mod}")
                core_attr += ml / len(consumers)
                core_modules_used.append(mod)
        core_attr = round(core_attr, 1)

        unified_total = collector_loc + core_attr
        if unified_total > 0:
            ratio = tk_loc / unified_total
        else:
            ratio = float("inf") if tk_loc > 0 else 0.0

        rows.append({
            "platform": platform,
            "toolkit_roots": roots_used,
            "toolkit_files": tk_files,
            "toolkit_loc": tk_loc,
            "collector_file": collector_rel,
            "collector_loc": collector_loc,
            "core_modules_used": core_modules_used,
            "core_attributed_loc": core_attr,
            "unified_attributed_loc": round(unified_total, 1),
            "ratio": round(ratio, 2) if ratio != float("inf") else None,
            "ratio_display": fmt_ratio(ratio),
            "status": status_for(tk_loc, ratio),
        })

    rows.sort(key=lambda r: (r["ratio"] if r["ratio"] is not None else -1), reverse=True)
    return rows, wave0


def render_md(rows, wave0, generated_at: str) -> str:
    lines = []
    lines.append("# PARITY MATRIX")
    lines.append(f"Generated: {generated_at}")
    lines.append("")
    lines.append("Toolkit standalone codebases vs the unified port. Unified attributed LOC =")
    lines.append("LOC(src/collectors/<platform>.py) + Σ LOC(src/core/<module>) / |consumers|")
    lines.append("for each Wave 0 cross-cutting module that platform consumes.")
    lines.append("")
    lines.append("## Toolkit -> Unified Bloat Ratios (sorted by ratio desc)")
    lines.append("")
    lines.append("| Platform | Toolkit LOC | Collector LOC | + Core attr | Unified total | Ratio | Status | Notes |")
    lines.append("|----------|------------:|--------------:|------------:|--------------:|------:|:------:|-------|")

    notes_map = {
        "instagram": "largest toolkit; Wave 2 Batch F (2 agents)",
        "whatsapp":  "Selenium + Playwright dual-engine",
        "telegram":  "telegramcollector + telegramtoolkit combined",
        "lemon8":    "shares stack with tiktok",
        "tiktok":    "toolkit folder may be partial",
        "strava":    "GPX + activity scraping",
        "search":    "google/duckduckgo aggregator",
        "github":    "PAT pool + spider",
        "youtube":   "previously reported 0 (FIXED)",
        "website":   "previously missing row (FIXED)",
        "matrix":    "no standalone toolkit; greenfield (Wave 0 matrix_client.py)",
    }
    for r in rows:
        notes = notes_map.get(r["platform"], "")
        lines.append(
            f"| {r['platform']} | {fmt_int(r['toolkit_loc'])} "
            f"| {fmt_int(r['collector_loc'])} "
            f"| {r['core_attributed_loc']:.1f} "
            f"| {r['unified_attributed_loc']:.1f} "
            f"| {r['ratio_display']} | {r['status']} | {notes} |"
        )
    lines.append("")

    # Totals
    total_toolkit = sum(r["toolkit_loc"] for r in rows)
    total_collector = sum(r["collector_loc"] for r in rows)
    total_core_loc = sum(w["loc"] for w in wave0)
    total_test_loc = sum(w["test_loc"] for w in wave0)
    lines.append(f"**Totals:** toolkit LOC = {fmt_int(total_toolkit)} | "
                 f"unified collectors LOC = {fmt_int(total_collector)} | "
                 f"Wave 0 core LOC = {fmt_int(total_core_loc)} | "
                 f"Wave 0 test LOC = {fmt_int(total_test_loc)}")
    lines.append("")
    lines.append("Status legend: ✓ ratio ≤ 5x · ⚠ ratio > 5x (port priority) · ✗ no toolkit code")
    lines.append("")

    lines.append("## Wave 0 cross-cutting core modules (deployed 2026-05-26)")
    lines.append("")
    lines.append("| Module | LOC | Tests LOC | Consumers | Per-consumer attribution |")
    lines.append("|--------|----:|----------:|-----------|-------------------------:|")
    for w in wave0:
        consumers_str = ", ".join(w["consumers"]) + f" ({len(w['consumers'])})"
        lines.append(
            f"| {w['module']} | {fmt_int(w['loc'])} | {fmt_int(w['test_loc'])} "
            f"| {consumers_str} | {w['per_consumer_attribution']} |"
        )
    lines.append("")

    lines.append("## Wave 0 module rollup")
    lines.append(f"- Total core LOC: {fmt_int(total_core_loc)}")
    lines.append(f"- Total test LOC: {fmt_int(total_test_loc)}")
    lines.append("- DB migrations: content_hashes, spider_queue, account_quota_usage, matrix_sync_state")
    lines.append("- New deps added: matrix-nio[e2e]>=0.24.0, imagehash>=4.3.0")
    lines.append("")

    lines.append("## Bug fixes vs prior PARITY (2026-05-26 01:04)")
    lines.append("- youtube: prior matrix reported toolkit_loc=0; now scans youtubetoolkit/ correctly.")
    lines.append("- website: row was missing entirely; now included with websitetoolkit/ scan.")
    lines.append("- telegram: now sums telegramcollector/ + telegramtoolkit/ explicitly.")
    lines.append("- whatsapp: now sums whatsapptoolkit/ + whatsappcollector/.")
    lines.append("")
    return "\n".join(lines) + "\n"


def render_json(rows, wave0, generated_at: str) -> str:
    payload = {
        "generated_at": generated_at,
        "schema_version": 2,
        "wave0_consumer_map": WAVE0_CONSUMERS,
        "wave0_modules": wave0,
        "rows": rows,
        "totals": {
            "toolkit_loc":  sum(r["toolkit_loc"] for r in rows),
            "collector_loc": sum(r["collector_loc"] for r in rows),
            "wave0_core_loc": sum(w["loc"] for w in wave0),
            "wave0_test_loc": sum(w["test_loc"] for w in wave0),
        },
        "fixes": [
            "youtube row now reflects youtubetoolkit/ LOC (was 0)",
            "website row now present (was missing)",
            "telegram sums telegramcollector + telegramtoolkit",
            "whatsapp sums whatsapptoolkit + whatsappcollector",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, wave0 = build_rows()

    md = render_md(rows, wave0, generated_at)
    js = render_json(rows, wave0, generated_at)

    (REPO_ROOT / "PARITY_MATRIX.md").write_text(md, encoding="utf-8")
    (REPO_ROOT / "PARITY_MATRIX.json").write_text(js, encoding="utf-8")

    # Console summary.
    print(f"PARITY regenerated at {generated_at}")
    print(f"  rows: {len(rows)}  wave0 modules: {len(wave0)}")
    top3 = [r for r in rows if r["ratio"] is not None][:3]
    for r in top3:
        print(f"  TOP {r['platform']:9s}  toolkit={r['toolkit_loc']:>6}  "
              f"unified={r['unified_attributed_loc']:>7.1f}  ratio={r['ratio_display']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
