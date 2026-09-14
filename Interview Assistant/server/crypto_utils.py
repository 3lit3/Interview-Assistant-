"""Server-side key issuance: encrypt LLM API key per-device.

Security model:
  - Real API key lives ONLY on the server (env var LLM_API_KEY).
  - Client never receives plaintext key. Gets an encrypted blob bound to
    their HWID, decryptable only on that device.
  - PBKDF2-HMAC-SHA256 derives a 32-byte AES key from HWID + server secret.
  - AES-CBC encrypts the API key. Blob = IV (16B) + ciphertext.
  - Verification hash = HMAC(SHA256) of HWID, so client can confirm blob
    is for THIS device (prevents mix-ups, not security).
  - Server secret is in env var KEY_ISSUANCE_SECRET.

No external crypto deps: uses hashlib + hmac (stdlib).
AES implementation is inline to avoid adding 'cryptography' to server deps.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import struct


def _derive_key(hwid: str, secret: str) -> bytes:
    """PBKDF2-HMAC-SHA256: 100k iterations, 32-byte key."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        (hwid + secret).encode("utf-8"),
        hwid[:16].encode("utf-8"),
        iterations=100_000,
        dklen=32,
    )


# ── Minimal AES-256-CBC (PKCS7 padding) ──────────────────────────
# Inline to avoid external dependency. Covers encrypt only (server-side).

_SBOX = [
    0x63,0x7C,0x77,0x7B,0xF2,0x6B,0x6F,0xC5,0x30,0x01,0x67,0x2B,0xFE,0xD7,0xAB,0x76,
    0xCA,0x82,0xC9,0x7D,0xFA,0x59,0x47,0xF0,0xAD,0xD4,0xA2,0xAF,0x9C,0xA4,0x72,0xC0,
    0xB7,0xFD,0x93,0x26,0x36,0x3F,0xF7,0xCC,0x34,0xA5,0xE5,0xF1,0x71,0xD8,0x31,0x15,
    0x04,0xC7,0x23,0xC3,0x18,0x96,0x05,0x9A,0x07,0x12,0x80,0xE2,0xEB,0x27,0xB2,0x75,
    0x09,0x83,0x2C,0x1A,0x1B,0x6E,0x5A,0xA0,0x52,0x3B,0xD6,0xB3,0x29,0xE3,0x2F,0x84,
    0x53,0xD1,0x00,0xED,0x20,0xFC,0xB1,0x5B,0x6A,0xCB,0xBE,0x39,0x4A,0x4C,0x58,0xCF,
    0xD0,0xEF,0xAA,0xFB,0x43,0x4D,0x33,0x85,0x45,0xF9,0x02,0x7F,0x50,0x3C,0x9F,0xA8,
    0x51,0xA3,0x40,0x8F,0x92,0x9D,0x38,0xF5,0xBC,0xB6,0xDA,0x21,0x10,0xFF,0xF3,0xD2,
    0xCD,0x0C,0x13,0xEC,0x5F,0x97,0x44,0x17,0xC4,0xA7,0x7E,0x3D,0x64,0x5D,0x19,0x73,
    0x60,0x81,0x4F,0xDC,0x22,0x2A,0x90,0x88,0x46,0xEE,0xB8,0x14,0xDE,0x5E,0x0B,0xDB,
    0xE0,0x32,0x3A,0x0A,0x49,0x06,0x24,0x5C,0xC2,0xD3,0xAC,0x62,0x91,0x95,0xE4,0x79,
    0xE7,0xC8,0x37,0x6D,0x8D,0xD5,0x4E,0xA9,0x6C,0x56,0xF4,0xEA,0x65,0x7A,0xAE,0x08,
    0xBA,0x78,0x25,0x2E,0x1C,0xA6,0xB4,0xC6,0xE8,0xDD,0x74,0x1F,0x4B,0xBD,0x8B,0x8A,
    0x70,0x3E,0xB5,0x66,0x48,0x03,0xF6,0x0E,0x61,0x35,0x57,0xB9,0x86,0xC1,0x1D,0x9E,
    0xE1,0xF8,0x98,0x11,0x69,0xD9,0x8E,0x94,0x9B,0x1E,0x87,0xE9,0xCE,0x55,0x28,0xDF,
    0x8C,0xA1,0x89,0x0D,0xBF,0xE6,0x42,0x68,0x41,0x99,0x2D,0x0F,0xB0,0x54,0xBB,0x16,
]

_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1B,0x36]

def _xtime(a):
    return ((a << 1) ^ 0x11B) & 0xFF

def _mix_single(a):
    return _xtime(a) ^ (_xtime(a) ^ a) ^ a ^ a  # simplified; use proper mix below

def _sub_bytes(s):
    return [_SBOX[b] for b in s]

def _shift_rows(s):
    return (
        [s[0],s[5],s[10],s[15], s[4],s[9],s[14],s[3],
         s[8],s[13],s[2],s[7], s[12],s[1],s[6],s[11]]
    )

def _mix_columns(s):
    r = [0]*16
    for c in range(4):
        i = c*4
        a = s[i:i+4]
        r[i]   = _xtime(a[0]) ^ (_xtime(a[1])^a[1]) ^ a[2] ^ a[3]
        r[i+1] = a[0] ^ _xtime(a[1]) ^ (_xtime(a[2])^a[2]) ^ a[3]
        r[i+2] = a[0] ^ a[1] ^ _xtime(a[2]) ^ (_xtime(a[3])^a[3])
        r[i+3] = (_xtime(a[0])^a[0]) ^ a[1] ^ a[2] ^ _xtime(a[3])
    return r

def _add_round_key(s, rk):
    return [a ^ b for a, b in zip(s, rk)]

def _key_expansion(key: bytes) -> list[list[int]]:
    nk = 8  # 256-bit
    nr = 14
    w = [list(key[i:i+4]) for i in range(0, 32, 4)]
    for i in range(nk, 4*(nr+1)):
        temp = w[i-1][:]
        if i % nk == 0:
            temp = [_SBOX[b] for b in [temp[1],temp[2],temp[3],temp[0]]]
            temp[0] ^= _RCON[i//nk - 1]
        elif i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        w.append([a ^ b for a, b in zip(w[i-nk], temp)])
    return [w[i*4:(i+1)*4] for i in range(nr+1)]

def _aes_encrypt_block(block: bytes, expanded_key: list) -> bytes:
    nr = 14
    state = list(block)
    state = _add_round_key(state, [b for rk in expanded_key[0] for b in rk])
    for r in range(1, nr):
        state = _sub_bytes(state)
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = _add_round_key(state, [b for rk in expanded_key[r] for b in rk])
    state = _sub_bytes(state)
    state = _shift_rows(state)
    state = _add_round_key(state, [b for rk in expanded_key[nr] for b in rk])
    return bytes(state)

def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)

def _aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    expanded = _key_expansion(key)
    padded = _pkcs7_pad(plaintext)
    prev = iv
    out = b""
    for i in range(0, len(padded), 16):
        block = bytes(a ^ b for a, b in zip(padded[i:i+16], prev))
        encrypted = _aes_encrypt_block(block, expanded)
        out += encrypted
        prev = encrypted
    return out
# ── end AES ───────────────────────────────────────────────────────


def issue_encrypted_key(hwid: str, api_key: str) -> dict:
    """Encrypt API key for a specific device. Returns transport dict."""
    secret = os.getenv("KEY_ISSUANCE_SECRET", "")
    if not secret:
        raise RuntimeError("KEY_ISSUANCE_SECRET not set on server")

    aes_key = _derive_key(hwid, secret)
    iv = secrets.token_bytes(16)
    ciphertext = _aes_cbc_encrypt(api_key.encode("utf-8"), aes_key, iv)

    blob = iv.hex() + ":" + ciphertext.hex()

    verification = hmac.new(
        aes_key, hwid.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]

    return {
        "blob": blob,
        "verification": verification,
        "algo": "pbkdf2sha256+aes256cbc",
    }
