import os
import pickle

import auth_cache


def test_load_cached_credentials_migrates_legacy_token_filename(tmp_path, monkeypatch):
    legacy_path = tmp_path / "token.json"
    new_path = tmp_path / "oauth_credentials.pickle"
    app_data_dir = tmp_path
    credentials = {"access_token": "abc123"}

    with legacy_path.open("wb") as handle:
        pickle.dump(credentials, handle)

    monkeypatch.setattr(auth_cache, "APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(auth_cache, "TOKEN_FILE", legacy_path)
    monkeypatch.setattr(auth_cache, "OAUTH_CREDENTIALS_FILE", new_path)

    loaded = auth_cache.load_cached_credentials()

    assert loaded == credentials
    assert new_path.exists()
    assert not legacy_path.exists()

    with new_path.open("rb") as handle:
        assert pickle.load(handle) == credentials
