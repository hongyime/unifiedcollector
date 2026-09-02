"""ghunt_enrich — CREDENTIAL-GATED scaffold for GHunt (email -> Google account) OSINT.

STATE: SCAFFOLD ONLY. This module does NOTHING without operator-provided GHunt
credentials. It NEVER fabricates a Google profile. Off by default.

--- What GHunt is ---
GHunt (github.com/mxrch/GHunt) is an offensive Google OSINT framework. Given a
Gmail address it queries Google's internal People / Gaia / Drive endpoints and
returns the Google Account attached (Gaia ID, display name, profile photo,
public reviews, YouTube channel, calendar, etc.) — the same surface `osint.
industries` sells commercially. It is an UNOFFICIAL client of Google APIs.

--- Auth (confirmed from upstream, master branch, README + ghunt/objects/base.py) ---
GHunt REQUIRES a live authenticated Google session. There is no anonymous mode.
Credentials are (a) master_token from an Android auth flow, (b) a set of Google
web cookies (SID/HSID/SSID/APISID/SAPISID/etc.), and (c) a bundle of OSIDs.
They are generated ONCE via `ghunt login` — three interactive options:
    1) Companion browser extension (Firefox/Chrome, ghunt-companion) listener,
    2) base64-encoded cookies pasted in from the same extension,
    3) manual cookie entry.
The result is written to `~/.malfrats/ghunt/creds.m` (JSON). Every ghunt CLI
call reads that file. There is no `GHUNT_CREDS` env var upstream — the file
path is fixed, so we gate on presence of the file *or* an env-configured
mount path. Creds expire and must be regenerated when Google rotates the
session.

--- CLI contract we depend on ---
    ghunt email <address> --json <output.json>

produces a JSON dump with the discovered profile. We shell out to it and read
the JSON — no need to link the ghunt Python package into the collector image.

--- ToS / legal / operational caveats (operator MUST read) ---
- GHunt hits UNOFFICIAL Google endpoints. This likely violates Google's ToS.
  Use a THROWAWAY Google account whose loss you accept. Do NOT use a
  primary/work account — session revocation on abuse is realistic.
- Rate limits are undocumented; abusive volume can trip Google anti-abuse and
  invalidate creds mid-run. Keep call rates low (seconds between lookups) and
  cache aggressively.
- Output is enrichment. It confirms "this email belongs to a real Gaia ID
  named X with photo Y" — that's evidence, not automatic identity linking.
  Feed it into the analyzer at the same tier as device/phone-OSINT metadata.
- Under some jurisdictions, systematic collection of profile data on
  individuals may trigger privacy law (GDPR/PDPA). Operator responsibility.

--- Operator setup (leave OFF until ready) ---
    1) Install ghunt in a throwaway env:
           pipx install ghunt   # (or pip install ghunt in a venv)
    2) Install the GHunt Companion browser extension (Firefox or Chrome).
    3) Run `ghunt login`, choose option 1 (Companion listener), sign into
       your THROWAWAY Google account in the extension, click "Send to GHunt".
       This writes ~/.malfrats/ghunt/creds.m.
    4) Copy creds.m into a location the collector can read, and export:
           GHUNT_CREDS=/path/to/creds.m
           GHUNT_BIN=/path/to/ghunt        # optional; default is `ghunt` on PATH
       (This module refuses to run without both a valid path AND the ghunt
       binary being resolvable.)
    5) NEVER commit creds.m. Rotate whenever a run fails auth.

--- No-op contract ---
If GHUNT_CREDS is not set OR the file doesn't exist OR the ghunt binary is
not on PATH: `run_lookup()` returns a status dict with `status="skipped"` and
a human-readable `reason`, and NOTHING is executed. This module NEVER makes
up Google profile data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

_ENV_CREDS = "GHUNT_CREDS"       # path to creds.m
_ENV_BIN = "GHUNT_BIN"           # optional override; else `ghunt` on PATH
_DEFAULT_TIMEOUT_S = 60.0


def _resolve_bin() -> str | None:
    """Return the ghunt executable path, or None if not installed."""
    explicit = os.environ.get(_ENV_BIN)
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    return shutil.which("ghunt")


def _creds_path() -> str | None:
    """Return the operator-configured creds.m path, or None if not set/missing."""
    p = os.environ.get(_ENV_CREDS)
    if not p:
        return None
    return p if os.path.isfile(p) else None


def is_configured() -> tuple[bool, str]:
    """Return (ready, reason). `ready=True` iff both creds file AND ghunt bin
    are resolvable. No side effects."""
    if not (creds := _creds_path()):
        return False, f"{_ENV_CREDS} not set or file missing — GHunt disabled"
    if not (binp := _resolve_bin()):
        return False, "ghunt binary not on PATH (set GHUNT_BIN or `pipx install ghunt`)"
    return True, f"ready (creds={creds}, bin={binp})"


async def run_lookup(email: str, *, timeout: float = _DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Enrich a single email via GHunt if creds are configured, else skip.

    Return shape (always):
        {
          "email":  "<echo>",
          "status": "ok" | "skipped" | "error",
          "reason": "<why, if not ok>",
          "data":   {<ghunt json>}   # only when status=="ok"
        }

    NEVER raises for expected states (missing creds, missing binary, ghunt
    non-zero exit). Only unexpected asyncio/subprocess errors propagate.
    """
    ready, reason = is_configured()
    if not ready:
        return {"email": email, "status": "skipped", "reason": reason}

    binp = _resolve_bin()
    assert binp is not None  # is_configured() proved this
    creds = _creds_path()
    assert creds is not None  # is_configured() proved this

    # ghunt hardcodes `Path().home() / '.malfrats/ghunt/creds.m'` in
    # ghunt/objects/base.py — there is NO env var to point it at a custom path.
    # AND ghunt REWRITES creds.m after every auth (rotating web tokens), so we
    # cannot symlink at a read-only bind mount — ghunt would EROFS on
    # save_creds(). Instead we COPY the operator's creds into a per-call HOME,
    # let ghunt refresh its in-memory tokens there, and DISCARD the writable
    # copy at the end. The authoritative creds.m stays on the host, read-only
    # bind-mounted, and untouched. When it eventually expires operator runs
    # `ghunt login` again to regenerate — same operational contract as before.
    import shutil as _sh
    staged_home = tempfile.mkdtemp(prefix="ghunt-home-")
    staged_dir = os.path.join(staged_home, ".malfrats", "ghunt")
    os.makedirs(staged_dir, exist_ok=True)
    staged_creds = os.path.join(staged_dir, "creds.m")
    _sh.copyfile(creds, staged_creds)
    env = os.environ.copy()
    env["HOME"] = staged_home

    # ghunt writes JSON to a path; use a tempfile per call.
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
        out_path = tf.name
    try:
        proc = await asyncio.create_subprocess_exec(
            binp, "email", email, "--json", out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"email": email, "status": "error", "reason": f"timeout after {timeout}s"}
        if proc.returncode != 0:
            return {
                "email": email,
                "status": "error",
                "reason": f"ghunt exit {proc.returncode}: {stderr.decode(errors='replace')[:400]}",
            }
        try:
            with open(out_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return {"email": email, "status": "error", "reason": f"json read failed: {e!r}"}
        return {"email": email, "status": "ok", "reason": "", "data": data}
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        # Clean the staged HOME (symlink + parent dirs).
        try:
            import shutil as _sh
            _sh.rmtree(staged_home, ignore_errors=True)
        except Exception:
            pass

def main() -> None:
    """CLI shim for smoke-testing the gate. Never invokes ghunt without creds.

        python -m src.core.ghunt_enrich <email>     # single lookup, or skip
        python -m src.core.ghunt_enrich --status    # print is_configured()
    """
    import argparse

    ap = argparse.ArgumentParser(description="Credential-gated GHunt scaffold")
    ap.add_argument("email", nargs="?", help="email to look up (optional)")
    ap.add_argument("--status", action="store_true", help="only report readiness")
    args = ap.parse_args()

    ready, reason = is_configured()
    if args.status or not args.email:
        print(json.dumps({"ready": ready, "reason": reason}, indent=2))
        return
    if not ready:
        print(json.dumps({"email": args.email, "status": "skipped", "reason": reason}, indent=2))
        return
    result = asyncio.run(run_lookup(args.email))
    # Trim `data` for CLI readability; full payload still returned by run_lookup().
    if result.get("status") == "ok":
        result = {**result, "data": "<omitted; use run_lookup() from code>"}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
