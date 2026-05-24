from __future__ import annotations

import base64
import os
from dataclasses import dataclass

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover - exercised when dependency is missing at runtime
    AESGCM = None


class SecretCryptoError(RuntimeError):
    """Raised when secret encryption/decryption cannot be performed."""


@dataclass(frozen=True)
class EncryptedSecret:
    """Encrypted secret payload suitable for DB persistence."""

    ciphertext: bytes
    nonce: bytes
    auth_tag: bytes


class SecretCipher:
    """AES-GCM based helper for encrypting and decrypting control-plane secrets."""

    def __init__(self, key_material: str, key_id: str = "local-kek-v1") -> None:
        if AESGCM is None:
            raise SecretCryptoError(
                "cryptography is required for control-plane secret encryption. "
                "Install collector dependencies before using secret storage."
            )

        self.key_id = (key_id or "local-kek-v1").strip()
        self._key = self._decode_key_material(key_material)
        self._aead = AESGCM(self._key)

    @staticmethod
    def _decode_key_material(key_material: str) -> bytes:
        raw = (key_material or "").strip()
        if not raw:
            raise SecretCryptoError(
                "CONTROL_PLANE_SECRET_KEY is required for encrypted secret storage."
            )

        # Accept URL-safe/base64 encoded key material.
        padded = raw + "=" * ((4 - len(raw) % 4) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
            if len(decoded) in (16, 24, 32):
                return decoded
        except Exception:
            pass

        # Also accept direct raw AES key bytes in env (16/24/32 chars).
        raw_bytes = raw.encode("utf-8")
        if len(raw_bytes) in (16, 24, 32):
            return raw_bytes

        raise SecretCryptoError(
            "CONTROL_PLANE_SECRET_KEY must be a 16/24/32-byte raw key or base64/urlsafe-base64 encoded key."
        )

    def encrypt(self, plaintext: str, associated_data: bytes | None = None) -> EncryptedSecret:
        value = "" if plaintext is None else str(plaintext)
        nonce = os.urandom(12)
        combined = self._aead.encrypt(nonce, value.encode("utf-8"), associated_data)
        # AESGCM returns ciphertext || 16-byte authentication tag.
        return EncryptedSecret(ciphertext=combined[:-16], nonce=nonce, auth_tag=combined[-16:])

    def decrypt(self, payload: EncryptedSecret, associated_data: bytes | None = None) -> str:
        combined = payload.ciphertext + payload.auth_tag
        plaintext = self._aead.decrypt(payload.nonce, combined, associated_data)
        return plaintext.decode("utf-8")


def masked_secret(value: str | None) -> str:
    """Return a consistently masked placeholder for secret values."""
    return "********" if value is not None else "(unset)"
