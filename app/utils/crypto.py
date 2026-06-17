"""Symmetric encryption for secrets at rest (clone-bot tokens).

Uses Fernet from ``cryptography`` (already a project dependency). The master key comes
from ``settings.CLONE_TOKEN_SECRET``. To avoid key-format friction for operators, the
secret may be either a real urlsafe-base64 32-byte Fernet key OR any passphrase — in the
latter case a stable Fernet key is derived from it via SHA-256. Same secret in → same key.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


@lru_cache(maxsize=4)
def _fernet_for(secret: str) -> Fernet:
    raw = secret.strip()
    # Accept a ready-made Fernet key (urlsafe-base64 of exactly 32 bytes) as-is.
    try:
        decoded = base64.urlsafe_b64decode(raw.encode('ascii'))
        if len(decoded) == 32:
            return Fernet(raw.encode('ascii'))
    except Exception:
        pass
    # Otherwise derive a deterministic 32-byte key from the passphrase.
    digest = hashlib.sha256(raw.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> Fernet:
    secret = (settings.CLONE_TOKEN_SECRET or '').strip()
    if not secret:
        raise RuntimeError('CLONE_TOKEN_SECRET is not set — cannot encrypt/decrypt clone bot tokens')
    return _fernet_for(secret)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret (e.g. a bot token) for storage at rest."""
    return _fernet().encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a value produced by :func:`encrypt_secret`."""
    try:
        return _fernet().decrypt(ciphertext.encode('ascii')).decode('utf-8')
    except InvalidToken as exc:  # wrong key or corrupted payload
        raise ValueError('Failed to decrypt secret (wrong CLONE_TOKEN_SECRET?)') from exc
