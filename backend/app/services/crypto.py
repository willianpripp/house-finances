"""Fernet-based encryption for Plaid access_tokens stored in DB.

The key lives in FERNET_KEY (env). Never write the key to DB. Generate
one for first-time setup with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


_fernet: Fernet | None = None


def _instance() -> Fernet:
    global _fernet
    if _fernet is None:
        if not settings.fernet_key:
            raise RuntimeError(
                "FERNET_KEY not configured. Generate one via "
                "`python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"` and set in .env."
            )
        _fernet = Fernet(settings.fernet_key.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    return _instance().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _instance().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Failed to decrypt — FERNET_KEY may have changed since the "
            "token was stored. Re-link the affected Plaid Item."
        ) from exc
