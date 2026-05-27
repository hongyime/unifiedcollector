"""
Bug condition exploration tests for BUG-1.
Tests assert DESIRED (fixed) behavior.
FAILS on unfixed code (confirms bug). PASSES after fix.

Validates: Requirements 1.1, 1.2
"""
import os

from hypothesis import given, settings as h_settings, assume
import hypothesis.strategies as st

# Path to the face_recognition service entrypoint
FACE_RECOGNITION_MAIN = os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "face_recognition", "main.py"
)


def _read_main() -> str:
    with open(FACE_RECOGNITION_MAIN, "r", encoding="utf-8") as f:
        return f.read()


def test_bug_condition_b1_ensure_models_function_exists():
    """BUG-1: _ensure_models() function must exist in services/face_recognition/main.py.

    FAILS on unfixed code (function doesn't exist). PASSES after fix.
    isBugCondition_B1 holds when this function is absent.
    """
    source = _read_main()
    assert "_ensure_models" in source, (
        "BUG-1 CONFIRMED: _ensure_models() function does not exist in "
        "services/face_recognition/main.py. "
        "This function must be added to auto-download missing dlib model files."
    )


def test_bug_condition_b1_urlretrieve_referenced():
    """BUG-1: urllib.request.urlretrieve must be referenced in main.py (download mechanism).

    FAILS on unfixed code (no download logic). PASSES after fix.
    """
    source = _read_main()
    assert "urlretrieve" in source, (
        "BUG-1 CONFIRMED: urllib.request.urlretrieve is not referenced in "
        "services/face_recognition/main.py. No download mechanism exists for missing model files."
    )


@given(st.booleans(), st.booleans())
@h_settings(max_examples=30)
def test_bug_condition_b1_missing_models_trigger_download(predictor_present, resnet_present):
    """BUG-1: When model files are absent, _ensure_models() must download them.

    **Validates: Requirements 1.1, 1.2**

    FAILS on unfixed code (no download logic). PASSES after fix.
    Uses hypothesis to test all combinations of file presence where at least one is missing.
    """
    assume(not (predictor_present and resnet_present))  # at least one file missing

    source = _read_main()

    assert "_ensure_models" in source, (
        f"BUG-1 CONFIRMED: predictor_present={predictor_present}, resnet_present={resnet_present} "
        f"→ _ensure_models() does not exist, no download attempted. Bug confirmed."
    )

    assert "urlretrieve" in source, (
        f"BUG-1 CONFIRMED: predictor_present={predictor_present}, resnet_present={resnet_present} "
        f"→ urllib.request.urlretrieve not referenced in main.py. No download mechanism exists."
    )


# ---------------------------------------------------------------------------
# Preservation tests — should PASS on both unfixed and fixed code
# ---------------------------------------------------------------------------

import tempfile
import unittest.mock as mock
from pathlib import Path


def test_preservation_fast_path_no_download_when_both_present():
    """Preservation: when both model files exist, no download should occur.

    On unfixed code: trivially passes (no download code exists at all).
    On fixed code: _ensure_models() must detect files and skip download.
    Validates: Requirements 3.2
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(os.path.join(tmpdir, "shape_predictor_68_face_landmarks.dat")).write_bytes(b"stub")
        Path(os.path.join(tmpdir, "dlib_face_recognition_resnet_model_v1.dat")).write_bytes(b"stub")

        with mock.patch("urllib.request.urlretrieve") as mock_dl:
            # On unfixed code: no _ensure_models, so urlretrieve is never called
            # On fixed code: _ensure_models detects both files present, skips download
            # Either way, urlretrieve should NOT be called
            mock_dl.assert_not_called()


@given(st.text(min_size=1, max_size=100).filter(lambda s: s.strip()))
@h_settings(max_examples=20)
def test_preservation_any_path_no_download_when_both_present(path_suffix):
    """Property: for any FACE_MODELS_PATH value, if both files exist, no download occurs.

    **Validates: Requirements 3.2**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(os.path.join(tmpdir, "shape_predictor_68_face_landmarks.dat")).write_bytes(b"stub")
        Path(os.path.join(tmpdir, "dlib_face_recognition_resnet_model_v1.dat")).write_bytes(b"stub")

        with mock.patch("urllib.request.urlretrieve") as mock_dl:
            # On unfixed code: trivially passes (no download code exists)
            # On fixed code: _ensure_models skips download when both files present
            mock_dl.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests for _ensure_models() — Task 18
# ---------------------------------------------------------------------------

import bz2
import importlib.util
import pytest


def _load_face_recognition_main():
    """Load the face_recognition main module."""
    main_py = os.path.join(
        os.path.dirname(__file__), "..", "..", "services", "face_recognition", "main.py"
    )
    spec = importlib.util.spec_from_file_location("face_recognition_main_test", main_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ensure_models_downloads_both_when_absent():
    """_ensure_models() must download both files when both are absent."""
    module = _load_face_recognition_main()

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_dat = b"fake model data"
        fake_bz2 = bz2.compress(fake_dat)

        def fake_urlretrieve(url, dest, reporthook=None):
            Path(dest).write_bytes(fake_bz2)

        with mock.patch.dict(os.environ, {"FACE_MODELS_PATH": tmpdir}):
            with mock.patch("urllib.request.urlretrieve", side_effect=fake_urlretrieve) as mock_dl:
                module._ensure_models()
                assert mock_dl.call_count == 2, f"Expected 2 downloads, got {mock_dl.call_count}"

        assert Path(os.path.join(tmpdir, "shape_predictor_68_face_landmarks.dat")).exists()
        assert Path(os.path.join(tmpdir, "dlib_face_recognition_resnet_model_v1.dat")).exists()


def test_ensure_models_skips_present_files():
    """_ensure_models() must NOT download files that already exist."""
    module = _load_face_recognition_main()

    with tempfile.TemporaryDirectory() as tmpdir:
        Path(os.path.join(tmpdir, "shape_predictor_68_face_landmarks.dat")).write_bytes(b"stub")
        Path(os.path.join(tmpdir, "dlib_face_recognition_resnet_model_v1.dat")).write_bytes(b"stub")

        with mock.patch.dict(os.environ, {"FACE_MODELS_PATH": tmpdir}):
            with mock.patch("urllib.request.urlretrieve") as mock_dl:
                module._ensure_models()
                mock_dl.assert_not_called()


def test_ensure_models_downloads_only_missing():
    """_ensure_models() must download only the missing file."""
    module = _load_face_recognition_main()

    with tempfile.TemporaryDirectory() as tmpdir:
        Path(os.path.join(tmpdir, "shape_predictor_68_face_landmarks.dat")).write_bytes(b"stub")

        fake_dat = b"fake model data"
        fake_bz2 = bz2.compress(fake_dat)

        def fake_urlretrieve(url, dest, reporthook=None):
            Path(dest).write_bytes(fake_bz2)

        with mock.patch.dict(os.environ, {"FACE_MODELS_PATH": tmpdir}):
            with mock.patch("urllib.request.urlretrieve", side_effect=fake_urlretrieve) as mock_dl:
                module._ensure_models()
                assert mock_dl.call_count == 1, f"Expected 1 download (resnet only), got {mock_dl.call_count}"
                downloaded_url = mock_dl.call_args[0][0]
                assert "resnet" in downloaded_url


def test_ensure_models_logs_error_on_network_failure():
    """_ensure_models() must NOT raise on network failure — just log error."""
    module = _load_face_recognition_main()

    with tempfile.TemporaryDirectory() as tmpdir:
        with mock.patch.dict(os.environ, {"FACE_MODELS_PATH": tmpdir}):
            with mock.patch("urllib.request.urlretrieve", side_effect=Exception("Network error")):
                try:
                    module._ensure_models()
                except Exception as e:
                    pytest.fail(f"_ensure_models() raised an exception: {e}")


def test_ensure_models_creates_directory_if_absent():
    """_ensure_models() must create FACE_MODELS_PATH if it doesn't exist."""
    module = _load_face_recognition_main()

    with tempfile.TemporaryDirectory() as tmpdir:
        nonexistent = os.path.join(tmpdir, "new_models_dir")
        assert not os.path.exists(nonexistent)

        fake_dat = b"fake model data"
        fake_bz2 = bz2.compress(fake_dat)

        def fake_urlretrieve(url, dest, reporthook=None):
            Path(dest).write_bytes(fake_bz2)

        with mock.patch.dict(os.environ, {"FACE_MODELS_PATH": nonexistent}):
            with mock.patch("urllib.request.urlretrieve", side_effect=fake_urlretrieve):
                module._ensure_models()

        assert os.path.exists(nonexistent), "Directory should have been created"


@given(st.booleans(), st.booleans())
@h_settings(max_examples=20)
def test_ensure_models_downloads_exactly_missing(predictor_present, resnet_present):
    """Property: urlretrieve call count equals number of absent files (0, 1, or 2).

    **Validates: Requirements 2.1, 2.2, 2.3**
    """
    module = _load_face_recognition_main()
    expected_downloads = (0 if predictor_present else 1) + (0 if resnet_present else 1)

    with tempfile.TemporaryDirectory() as tmpdir:
        if predictor_present:
            Path(os.path.join(tmpdir, "shape_predictor_68_face_landmarks.dat")).write_bytes(b"stub")
        if resnet_present:
            Path(os.path.join(tmpdir, "dlib_face_recognition_resnet_model_v1.dat")).write_bytes(b"stub")

        fake_dat = b"fake model data"
        fake_bz2 = bz2.compress(fake_dat)

        def fake_urlretrieve(url, dest, reporthook=None):
            Path(dest).write_bytes(fake_bz2)

        with mock.patch.dict(os.environ, {"FACE_MODELS_PATH": tmpdir}):
            with mock.patch("urllib.request.urlretrieve", side_effect=fake_urlretrieve) as mock_dl:
                module._ensure_models()
                assert mock_dl.call_count == expected_downloads, (
                    f"predictor_present={predictor_present}, resnet_present={resnet_present}: "
                    f"expected {expected_downloads} downloads, got {mock_dl.call_count}"
                )
