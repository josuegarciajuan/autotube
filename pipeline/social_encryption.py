"""Credential encryption for social media accounts.

Uses Fernet symmetric encryption (AES-128-GCM) with a master key
stored in ``config/social_encryption.key`` or the ``SOCIAL_ENCRYPTION_KEY``
environment variable.

Usage:
    from pipeline.social_encryption import get_encryption
    enc = get_encryption()
    ciphertext = enc.encrypt("my_password")
    plaintext = enc.decrypt(ciphertext)
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_KEY_FILE = Path(__file__).resolve().parent.parent / "config" / "social_encryption.key"
_ENV_VAR = "SOCIAL_ENCRYPTION_KEY"


class SocialCredentialEncryption:
    """Encrypt / decrypt social media credentials with Fernet."""

    def __init__(self, key: bytes | None = None):
        from cryptography.fernet import Fernet

        if key is None:
            key = self._load_or_generate_key()
        self._fernet = Fernet(key)

    # ── public API ──────────────────────────────────────────

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string → URL-safe base64 token."""
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a Fernet token → original plaintext."""
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except Exception:
            logger.error("Failed to decrypt credential (wrong key or corrupted data)")
            return ""

    # ── key management ─────────────────────────────────────

    @staticmethod
    def _load_or_generate_key() -> bytes:
        """Try env var → file → generate new key.

        Fernet() expects the key as a URL-safe base64 STRING (44 chars), not
        the raw decoded bytes. All load paths therefore return the string form
        after validating it decodes to 32 bytes.
        """
        from cryptography.fernet import Fernet

        # 1. env var
        env_val = os.getenv(_ENV_VAR)
        if env_val:
            try:
                raw = base64.urlsafe_b64decode(env_val.encode("utf-8"))
                if len(raw) == 32:
                    return env_val.encode("utf-8")
            except Exception:
                logger.warning("SOCIAL_ENCRYPTION_KEY is not valid base64 — generating new key")

        # 2. key file
        if _KEY_FILE.exists():
            try:
                with open(_KEY_FILE, "rb") as f:
                    key = f.read().strip()
                raw = base64.urlsafe_b64decode(key)
                if len(raw) == 32:
                    return key
            except Exception:
                logger.warning("social_encryption.key is corrupt — regenerating")

        # 3. generate new key
        key = Fernet.generate_key()
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(_KEY_FILE, 0o600)
        logger.info("Generated new social encryption key at %s", _KEY_FILE)
        return key


# ── singleton ─────────────────────────────────────────────

_encryption: SocialCredentialEncryption | None = None


def get_encryption() -> SocialCredentialEncryption:
    global _encryption
    if _encryption is None:
        _encryption = SocialCredentialEncryption()
    return _encryption
