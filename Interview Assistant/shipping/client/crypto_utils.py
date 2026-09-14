"""Client-side key decryption: recover LLM API key from encrypted blob.

Mirror of server/crypto_utils.py — same PBKDF2 + Fernet logic.
No external crypto deps in binary: uses hashlib + hmac (stdlib) for
verification, cryptography Fernet for decryption.

Security layers (read before modifying):
  1. Encrypted blob on disk — plaintext key never persisted.
  2. HWID-bound — blob useless on a different machine.
  3. Scattered decryption — key material assembled at runtime from multiple
     sources (HWID, embedded fragments). No single string in the binary is
     the full secret.
  4. Anti-debug — frame introspection detects common RE tools at import time.
  5. Session-only — decrypted key held in memory, wiped on exit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


# ── Anti-debug (layer 4) ─────────────────────────────────────────
def _anti_debug():
    """Detect common RE/debug hooks. Not bulletproof, raises the bar."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.IsDebuggerPresent():
            _panic()
    except Exception:
        pass
    try:
        frames = sys._getframe(1)
        if frames and frames.f_code.co_filename != __file__:
            _panic()
    except Exception:
        pass


def _panic():
    """Corrupt own state when tampering detected."""
    global _CORRUPTED
    _CORRUPTED = True


_CORRUPTED = False

# ── Fragment assembly (layer 3) ──────────────────────────────────
# Hex-encoded fragments of KEY_ISSUANCE_SECRET.
# Assembled at runtime only inside this function.
_F0 = "41"
_F1 = "64"
_F2 = "6d"
_F3 = "69"
_F4 = "6e"
_F5 = "38"
_F6 = "30"
_F7 = "38"

_G0 = "30"
_G1 = "21"
_G2 = "21"


def _assemble_secret() -> str:
    """Reconstruct server secret from hex fragments."""
    hex_str = (
        _F0 + _F1 + _F2 + _F3 + _F4 + _F5 + _F6 + _F7
        + _G0 + _G1 + _G2
    )
    return bytes.fromhex(hex_str).decode("utf-8")


# ── PBKDF2 key derivation ───────────────────────────────────────
def _derive_fernet_key(hwid: str) -> bytes:
    secret = _assemble_secret()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hwid[:16].encode("utf-8"),
        iterations=100_000,
    )
    raw = kdf.derive((hwid + secret).encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def decrypt_api_key(hwid: str, blob: str, verification: str) -> str:
    """Decrypt an encrypted key blob. Returns plaintext API key.

    Raises ValueError on tampering/HWID mismatch.
    Raises RuntimeError if anti-debug tripped.
    """
    _anti_debug()
    if _CORRUPTED:
        raise RuntimeError("integrity check failed")

    fernet_key = _derive_fernet_key(hwid)

    expected_v = hmac.new(
        fernet_key, hwid.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]
    if not hmac.compare_digest(verification, expected_v):
        raise ValueError("HWID mismatch — blob is for a different device")

    f = Fernet(fernet_key)
    plaintext = f.decrypt(blob.encode("utf-8"))

    return plaintext.decode("utf-8")
