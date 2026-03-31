"""Fernet symmetric encryption for OAuth tokens at rest.

Refresh tokens are encrypted before storage. The key is loaded from the
CREDENTIAL_ENCRYPTION_KEY environment variable. If unset, a random key is
generated per process (tokens will not survive restarts — suitable for dev only).
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_CIPHER: Fernet | None = None


def _get_cipher() -> Fernet:
    global _CIPHER
    if _CIPHER is None:
        raw = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
        if raw:
            _CIPHER = Fernet(raw.encode() if isinstance(raw, str) else raw)
        else:
            logger.warning(
                "CREDENTIAL_ENCRYPTION_KEY not set — generating ephemeral key. "
                "Tokens will NOT survive process restarts."
            )
            _CIPHER = Fernet(Fernet.generate_key())
    return _CIPHER


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt a token string for storage."""
    if not plaintext:
        return b""
    return _get_cipher().encrypt(plaintext.encode("utf-8"))


def decrypt_token(ciphertext: bytes) -> str:
    """Decrypt a stored token back to plaintext."""
    if not ciphertext:
        return ""
    if isinstance(ciphertext, str):
        ciphertext = ciphertext.encode("utf-8")
    return _get_cipher().decrypt(ciphertext).decode("utf-8")


def encrypt_token_str(plaintext: str) -> str:
    """Encrypt and return as string for JSON/SQLite text columns."""
    return encrypt_token(plaintext).decode("utf-8")
