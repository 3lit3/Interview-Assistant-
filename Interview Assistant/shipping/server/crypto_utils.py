"""Server-side key issuance: encrypt LLM API key per-device.

Security model:
  - Real API key lives ONLY on the server (env var LLM_API_KEY).
  - Client never receives plaintext key. Gets an encrypted blob bound to
    their HWID, decryptable only on that device.
  - PBKDF2-HMAC-SHA256 derives key from HWID + KEY_ISSUANCE_SECRET.
  - Fernet (AES-128-CBC + HMAC-SHA256) encrypts the API key.
  - Verification hash = HMAC(SHA256) of HWID for device-binding check.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def _derive_fernet_key(hwid: str) -> bytes:
    """PBKDF2 → 32 bytes → base64url (Fernet format)."""
    secret = os.getenv("KEY_ISSUANCE_SECRET", "")
    if not secret:
        raise RuntimeError("KEY_ISSUANCE_SECRET not set on server")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hwid[:16].encode("utf-8"),
        iterations=100_000,
    )
    raw = kdf.derive((hwid + secret).encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def issue_encrypted_key(hwid: str, api_key: str) -> dict:
    """Encrypt API key for a specific device. Returns transport dict."""
    fernet_key = _derive_fernet_key(hwid)
    f = Fernet(fernet_key)
    token = f.encrypt(api_key.encode("utf-8"))

    verification = hmac.new(
        fernet_key, hwid.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]

    return {
        "blob": token.decode("utf-8"),
        "verification": verification,
        "algo": "pbkdf2sha256+fernet",
    }
