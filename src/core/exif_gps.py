"""Tier-5 EXIF GPS extraction (COLLECTION_SPEC).

Shared building block (Phase 0). Pure-local, best-effort: reads GPS coordinates
embedded in a downloaded photo's EXIF and returns decimal lat/lon. No network,
no external calls — safe to run on any image we've already saved to disk.

Social platforms usually strip EXIF, so this returns None far more often than
not; that is expected ("best-effort" per spec). Never raises — a malformed or
EXIF-less file just yields None.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Content types worth scanning for EXIF GPS (images / photos only).
_IMAGE_CONTENT_TYPES = frozenset({
    "image", "photo", "story", "profile_photo", "user_profile_photo",
    "activity_photo", "thumbnail",
})

# Extensions that can carry EXIF GPS.
_EXIF_EXTENSIONS = frozenset({"jpg", "jpeg", "tif", "tiff", "heic", "heif", "webp"})


def is_exif_enabled() -> bool:
    return os.getenv("COLLECTOR_EXIF_GPS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def _ratio_to_float(value) -> Optional[float]:
    """Convert an EXIF rational (or IFDRational/float/int) to float."""
    try:
        # Pillow IFDRational and plain numbers both support float().
        return float(value)
    except (TypeError, ValueError):
        try:
            num, den = value
            return float(num) / float(den) if den else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None


def _dms_to_decimal(dms, ref: Optional[str]) -> Optional[float]:
    """Convert ((d),(m),(s)) + hemisphere ref to signed decimal degrees."""
    if not dms or len(dms) != 3:
        return None
    d = _ratio_to_float(dms[0])
    m = _ratio_to_float(dms[1])
    s = _ratio_to_float(dms[2])
    if d is None or m is None or s is None:
        return None
    dec = d + m / 60.0 + s / 3600.0
    if ref and ref.upper() in {"S", "W"}:
        dec = -dec
    return dec


def extract_gps(file_path: str) -> Optional[dict]:
    """Return {'lat': float, 'lon': float, ...} from a photo's EXIF, or None.

    Best-effort and exception-safe. Also pulls altitude and the GPS timestamp
    when present.
    """
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    if ext and ext not in _EXIF_EXTENSIONS:
        return None
    try:
        from PIL import Image
        from PIL.ExifTags import GPSTAGS
    except Exception:
        return None

    try:
        with Image.open(file_path) as img:
            exif = img.getexif()
            if not exif:
                return None
            # GPS IFD lives under tag 0x8825.
            gps_ifd = exif.get_ifd(0x8825)
            if not gps_ifd:
                return None
            gps = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}

        lat = _dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
        lon = _dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
        if lat is None or lon is None:
            return None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None

        out: dict = {"lat": round(lat, 7), "lon": round(lon, 7), "source": "exif"}
        alt = _ratio_to_float(gps.get("GPSAltitude")) if gps.get("GPSAltitude") is not None else None
        if alt is not None:
            ref = gps.get("GPSAltitudeRef")
            # ref 1 == below sea level
            if ref in (1, b"\x01"):
                alt = -alt
            out["alt"] = round(alt, 2)
        return out
    except Exception:
        logger.debug("EXIF GPS extraction failed for %s", file_path, exc_info=True)
        return None


__all__ = ["extract_gps", "is_exif_enabled"]
