"""Tests for src/core/exif_gps.py — Tier 5 EXIF GPS extraction."""
from __future__ import annotations

import os

import pytest

from src.core.exif_gps import extract_gps, is_exif_enabled


def _dms(deg: float):
    d = int(deg)
    m = int((deg - d) * 60)
    s = round(((((deg - d) * 60) - m) * 60) * 100)
    return ((d, 1), (m, 1), (s, 100))


def _write_jpeg_with_gps(path: str, lat: float, lon: float,
                         lat_ref: str = "N", lon_ref: str = "E") -> None:
    piexif = pytest.importorskip("piexif")
    from PIL import Image
    gps = {
        piexif.GPSIFD.GPSLatitudeRef: lat_ref,
        piexif.GPSIFD.GPSLatitude: _dms(abs(lat)),
        piexif.GPSIFD.GPSLongitudeRef: lon_ref,
        piexif.GPSIFD.GPSLongitude: _dms(abs(lon)),
    }
    exif_bytes = piexif.dump({"GPS": gps})
    Image.new("RGB", (8, 8), (120, 120, 120)).save(path, "jpeg", exif=exif_bytes)


def test_extract_gps_roundtrip(tmp_path):
    p = str(tmp_path / "geo.jpg")
    _write_jpeg_with_gps(p, 1.3521, 103.8198)  # Singapore
    r = extract_gps(p)
    assert r is not None
    assert abs(r["lat"] - 1.3521) < 0.01
    assert abs(r["lon"] - 103.8198) < 0.01
    assert r["source"] == "exif"


def test_extract_gps_southern_western_hemisphere(tmp_path):
    p = str(tmp_path / "geo2.jpg")
    _write_jpeg_with_gps(p, 33.8688, 151.2093, lat_ref="S", lon_ref="W")  # signs flip
    r = extract_gps(p)
    assert r is not None
    assert r["lat"] < 0  # S → negative
    assert r["lon"] < 0  # W → negative


def test_no_exif_returns_none(tmp_path):
    from PIL import Image
    p = str(tmp_path / "plain.jpg")
    Image.new("RGB", (8, 8)).save(p, "jpeg")
    assert extract_gps(p) is None


def test_non_image_extension_skipped(tmp_path):
    p = str(tmp_path / "doc.pdf")
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4 not an image")
    assert extract_gps(p) is None


def test_missing_file_returns_none():
    assert extract_gps("/nonexistent/path/x.jpg") is None


def test_is_exif_enabled_default_and_toggle(monkeypatch):
    monkeypatch.delenv("COLLECTOR_EXIF_GPS_ENABLED", raising=False)
    assert is_exif_enabled() is True
    monkeypatch.setenv("COLLECTOR_EXIF_GPS_ENABLED", "false")
    assert is_exif_enabled() is False
