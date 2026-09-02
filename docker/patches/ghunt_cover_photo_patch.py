"""Patch ghunt 2.3.4 parsers/people.py to survive missing `container` keys on
cover-photo entries. Upstream bug reproduced on multiple Google accounts:

    File ".../ghunt/parsers/people.py", line 164, in _scrape
        self.coverPhotos[cover_photo_data["metadata"]["container"]] = person_cover_photo
    KeyError: 'container'

Some People API responses omit `metadata.container` on cover-photo blobs;
ghunt then crashes before returning any profile data. This is a defensive
`.get()` guard around JUST the container-key lookup so the rest of the
profile parse (names, gaia id, profile photo) still runs.

Idempotent: safe to run repeatedly.  Applied inside the spiderfoot image so
the fix survives container recreate.
"""
from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path(
    "/opt/ghunt-venv/lib/python3.11/site-packages/ghunt/parsers/people.py"
)

OLD = (
    "            for cover_photo_data in person_data[\"coverPhoto\"]:\n"
    "                person_cover_photo = PersonPhoto()\n"
    "                await person_cover_photo._scrape(as_client, cover_photo_data, \"cover_photo\")\n"
    "                self.coverPhotos[cover_photo_data[\"metadata\"][\"container\"]] = person_cover_photo"
)

NEW = (
    "            for cover_photo_data in person_data[\"coverPhoto\"]:\n"
    "                # ghunt-cover-photo-patch: some People API responses omit\n"
    "                # metadata.container; skip those entries instead of crashing.\n"
    "                _container_key = (cover_photo_data.get(\"metadata\") or {}).get(\"container\")\n"
    "                if not _container_key:\n"
    "                    continue\n"
    "                person_cover_photo = PersonPhoto()\n"
    "                await person_cover_photo._scrape(as_client, cover_photo_data, \"cover_photo\")\n"
    "                self.coverPhotos[_container_key] = person_cover_photo"
)


def main() -> int:
    if not TARGET.is_file():
        print(f"ghunt_cover_photo_patch: target not found: {TARGET}", file=sys.stderr)
        return 1
    src = TARGET.read_text(encoding="utf-8")
    if "ghunt-cover-photo-patch" in src:
        print("ghunt_cover_photo_patch: already applied")
        return 0
    if OLD not in src:
        print("ghunt_cover_photo_patch: source drifted, cannot apply", file=sys.stderr)
        return 2
    TARGET.write_text(src.replace(OLD, NEW), encoding="utf-8")
    print("ghunt_cover_photo_patch: applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
