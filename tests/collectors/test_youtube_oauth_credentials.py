"""OAuth credential persistence stays private and JSON-only."""
from __future__ import annotations

import importlib.util
import json
import os
import pickle
from pathlib import Path

import pytest
from google.oauth2.credentials import Credentials

from src.collectors.youtube import _load_legacy_oauth_pickle, _write_private_oauth_json


def _bootstrap_module():
    path = Path(__file__).parents[2] / "scripts" / "youtube_oauth_bootstrap.py"
    spec = importlib.util.spec_from_file_location("youtube_oauth_bootstrap", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collector_oauth_json_write_is_private_and_atomic(tmp_path):
    path = tmp_path / "oauth.json"
    _write_private_oauth_json(path, '{"token":"redacted"}')

    assert json.loads(path.read_text()) == {"token": "redacted"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".oauth.json.*.tmp"))


def test_bootstrap_writes_private_json(tmp_path):
    module = _bootstrap_module()
    path = tmp_path / "oauth.json"
    module._write_private_json(path, '{"refresh_token":"redacted"}')

    assert json.loads(path.read_text()) == {"refresh_token": "redacted"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_bootstrap_has_no_pickle_loader():
    module = _bootstrap_module()
    assert not hasattr(module, "pickle")


def test_legacy_oauth_reader_accepts_a_google_credentials_pickle(tmp_path):
    path = tmp_path / "legacy.pickle"
    path.write_bytes(pickle.dumps(Credentials(token="redacted")))

    assert _load_legacy_oauth_pickle(path).token == "redacted"


def test_legacy_oauth_reader_rejects_non_google_globals(tmp_path):
    class _Malicious:
        def __reduce__(self):
            return (os.system, ("false",))

    path = tmp_path / "malicious.pickle"
    path.write_bytes(pickle.dumps(_Malicious()))

    with pytest.raises(pickle.UnpicklingError, match="Refusing to load"):
        _load_legacy_oauth_pickle(path)
