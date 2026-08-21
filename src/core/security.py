"""Lightweight helpers for secret handling (token files, not PII)."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography.fernet import Fernet

from src.core.config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    raw = settings.secret_encryption_key.encode("utf-8")
    # Derive a stable 32-byte url-safe key from whatever string is provided
    digest = hashlib.sha256(raw).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    return _fernet().decrypt(token)


def read_secret_file(path: str | Path) -> bytes | None:
    p = Path(path)
    if not p.exists():
        return None
    return p.read_bytes()
