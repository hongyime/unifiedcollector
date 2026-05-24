from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

COLLECTOR_ROOT = REPO_ROOT / "services" / "collector"
if str(COLLECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_ROOT))


from collector.control_plane_secrets import EncryptedSecret, SecretCipher, SecretCryptoError
from collector.database import Database


class _FakeCipher:
    key_id = "test-kek-v1"

    def __init__(self) -> None:
        self.last_decrypt_payload: EncryptedSecret | None = None
        self.last_decrypt_aad: bytes | None = None

    def encrypt(self, plaintext: str, associated_data: bytes | None = None) -> EncryptedSecret:
        # Deterministic fake payload to simplify SQL parameter assertions.
        return EncryptedSecret(ciphertext=b"\\xaa\\xbb", nonce=b"n" * 12, auth_tag=b"t" * 16)

    def decrypt(self, payload: EncryptedSecret, associated_data: bytes | None = None) -> str:
        self.last_decrypt_payload = payload
        self.last_decrypt_aad = associated_data
        return "super-secret-value"


def _mock_pool_with_connection(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def test_secret_cipher_round_trip():
    # 32-byte key encoded in URL-safe base64 (no padding form is accepted too).
    raw_key = os.urandom(32)
    b64_key = base64.urlsafe_b64encode(raw_key).decode("utf-8").rstrip("=")

    try:
        cipher = SecretCipher(b64_key, key_id="local-kek-v1")
    except SecretCryptoError as exc:
        if "cryptography is required" in str(exc):
            pytest.skip("cryptography not available in current test environment")
        raise

    encrypted = cipher.encrypt("my-secret", associated_data=b"collector:MEDIA_BRIDGE_SECRET")

    assert encrypted.ciphertext != b"my-secret"
    assert len(encrypted.nonce) == 12
    assert len(encrypted.auth_tag) == 16

    plaintext = cipher.decrypt(encrypted, associated_data=b"collector:MEDIA_BRIDGE_SECRET")
    assert plaintext == "my-secret"


def test_secret_cipher_rejects_invalid_key_material():
    with pytest.raises(SecretCryptoError):
        SecretCipher("not-a-valid-key")


def test_upsert_control_secret_encrypts_before_db_write():
    db = Database()
    fake_cipher = _FakeCipher()
    db._secret_cipher = fake_cipher

    mock_conn = AsyncMock()
    db.pool = _mock_pool_with_connection(mock_conn)

    asyncio.run(
        db.upsert_control_secret(
            service_name="collector",
            secret_key="MEDIA_BRIDGE_SECRET",
            plaintext_value="plain-text-should-not-hit-db",
            updated_by="operator@example.com",
            actor_role="operator",
            update_reason="rotate",
        )
    )

    # First execute call inserts into control_secret_values.
    first_call = mock_conn.execute.call_args_list[0]
    sql = first_call.args[0]
    params = first_call.args[1:]

    assert "control_secret_values" in sql
    assert params[0] == "collector"
    assert params[1] == "MEDIA_BRIDGE_SECRET"
    assert params[2] == b"\\xaa\\xbb"  # ciphertext
    assert params[3] == b"n" * 12  # nonce
    assert params[4] == b"t" * 16  # auth tag

    # Ensure plaintext is not passed into the DB value insert payload.
    assert "plain-text-should-not-hit-db" not in params


def test_get_control_secret_returns_masked_metadata_only():
    db = Database()

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "service_name": "collector",
            "secret_key": "MEDIA_BRIDGE_SECRET",
            "encryption_key_id": "local-kek-v1",
            "metadata": {"source": "dashboard"},
            "updated_by": "operator@example.com",
            "update_reason": "rotate",
            "updated_at": "2026-04-20T00:00:00Z",
        }
    )
    db.pool = _mock_pool_with_connection(mock_conn)

    result = asyncio.run(db.get_control_secret("collector", "MEDIA_BRIDGE_SECRET"))

    assert result is not None
    assert result["secret_key"] == "MEDIA_BRIDGE_SECRET"
    assert result["value_masked"] == "********"
    assert "plaintext" not in result


def test_get_control_secret_plaintext_requires_explicit_call():
    db = Database()
    fake_cipher = _FakeCipher()
    db._secret_cipher = fake_cipher

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "ciphertext": b"\\xaa\\xbb",
            "nonce": b"n" * 12,
            "auth_tag": b"t" * 16,
        }
    )
    db.pool = _mock_pool_with_connection(mock_conn)

    plaintext = asyncio.run(db.get_control_secret_plaintext("collector", "MEDIA_BRIDGE_SECRET"))

    assert plaintext == "super-secret-value"
    assert fake_cipher.last_decrypt_payload is not None
    assert fake_cipher.last_decrypt_payload.ciphertext == b"\\xaa\\xbb"
    assert fake_cipher.last_decrypt_aad == b"collector:MEDIA_BRIDGE_SECRET"
