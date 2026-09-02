"""Patch ghunt 2.3.4 modules/email.py to survive undefined `photos`/`reviews`
in the --json output path. Upstream bug:

    File ".../ghunt/modules/email.py", line 142, in hunt
        "photos": photos,
                  ^^^^^^
    NameError: name 'photos' is not defined

The maps JSON block references `photos` and `reviews`, but `gmaps.get_reviews`
only returns `(err, stats)`. Both names are stale from a previous gmaps API
shape. This patch injects `photos = None; reviews = None` right after the
gmaps call so the JSON dump is well-formed (values will show up as null).

Idempotent. Applied inside the spiderfoot image so the fix survives recreate.
"""
from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path(
    "/opt/ghunt-venv/lib/python3.11/site-packages/ghunt/modules/email.py"
)

OLD = (
    "    err, stats = await gmaps.get_reviews(as_client, target.personId)\n"
    "    gmaps.output(err, stats, target.personId)"
)

NEW = (
    "    err, stats = await gmaps.get_reviews(as_client, target.personId)\n"
    "    # ghunt-email-json-patch: upstream --json block references stale\n"
    "    # `photos`/`reviews` names that no gmaps API returns. Bind them here\n"
    "    # so the JSON dump serialises cleanly (null in output).\n"
    "    photos = None\n"
    "    reviews = None\n"
    "    gmaps.output(err, stats, target.personId)"
)


def main() -> int:
    if not TARGET.is_file():
        print(f"ghunt_email_json_patch: target not found: {TARGET}", file=sys.stderr)
        return 1
    src = TARGET.read_text(encoding="utf-8")
    if "ghunt-email-json-patch" in src:
        print("ghunt_email_json_patch: already applied")
        return 0
    if OLD not in src:
        print("ghunt_email_json_patch: source drifted, cannot apply", file=sys.stderr)
        return 2
    TARGET.write_text(src.replace(OLD, NEW), encoding="utf-8")
    print("ghunt_email_json_patch: applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
