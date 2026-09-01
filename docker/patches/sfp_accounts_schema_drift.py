#!/usr/bin/env python3
"""Idempotent patch for SpiderFoot v4.0's sfp_accounts module.

Fixes three drifts against the current upstream WhatsMyName data schema
(as of 2026-09; upstream README + wmn-data.json commits):

  1. Data URL moved: master/web_accounts_list.json -> main/wmn-data.json
  2. Sites list no longer has a `valid` boolean field (all listed are valid).
  3. Renames: `check_uri` -> `uri_check`, `category` -> `cat`.

Runs at image-build time from Dockerfile.spiderfoot AFTER SpiderFoot is
extracted to /opt/spiderfoot. Safe to re-run: each substitution is
idempotent (looks for the OLD token; no-op if already patched).

This does NOT enable sfp_accounts anywhere. It only makes the module
functional IF an operator later sets SPIDERFOOT_MODULES or
RECON_USERNAME_MODULES to include sfp_accounts. The existing policy gate
(SPIDERFOOT_ALLOW_INTRUSIVE, RECON_ALLOWLIST, per-target scope) is
unchanged.
"""
from __future__ import annotations

import pathlib
import re
import sys

MODULE_PATH = pathlib.Path("/opt/spiderfoot/modules/sfp_accounts.py")

OLD_URL = (
    "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/master/"
    "web_accounts_list.json"
)
NEW_URL = (
    "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/main/"
    "wmn-data.json"
)


def main() -> int:
    if not MODULE_PATH.exists():
        print(f"sfp_accounts patch: {MODULE_PATH} not found, skipping",
              file=sys.stderr)
        return 0
    source = MODULE_PATH.read_text()
    original = source

    # 1) Data URL.
    source = source.replace(OLD_URL, NEW_URL)

    # 2) `valid` field is gone from wmn-data.json.
    source = source.replace("if site['valid']", "if True")

    # 3) Field renames: check_uri -> uri_check, category -> cat.
    source = re.sub(r"(['\"])check_uri\1", r"\1uri_check\1", source)
    source = re.sub(r"(['\"])category\1", r"\1cat\1", source)

    if source == original:
        print("sfp_accounts patch: already applied (no-op)")
        return 0

    MODULE_PATH.write_text(source)
    print("sfp_accounts patch: applied schema-drift fixes "
          "(URL, valid-field, check_uri, category)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
