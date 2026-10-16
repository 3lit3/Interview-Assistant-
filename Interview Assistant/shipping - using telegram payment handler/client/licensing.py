"""Client licensing: signed offline keys, online once-per-device activation.

Model
-----
  - Keys are Ed25519-signed by the license server ($14.99 / 30 days). The
    client embeds the PUBLIC key only — nothing secret ships in the binary.
  - Signature + expiry are validated OFFLINE on every launch.
  - First use on a device calls POST /activate, which binds the key to this
    device (server enforces the 2-device cap). After that, launches work even
    when the license server is asleep.
  - Soft revocation: when the server is reachable, the watchdog asks
    /recheck; a revoked key shuts down. Network failures never do.

State lives in %APPDATA%\\InterviewAssistant\\license.json.
Deps: stdlib + cryptography (already bundled); the gate window reuses the
app's PyQt6 and lives in license_window.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PLAN_PRICE = "14.99"
PLAN_DAYS = 30
DEVICE_CAP = 2
RECHECK_MINUTES = 30        # watchdog interval for the soft recheck

# Ed25519 public verification key (raw 32 bytes, hex). Rotating it requires a
# new client build; the private key exists only on the license server.
PUBLIC_KEY_HEX = "c336325513196d7971034fc8fb329458c5ae0f98e81f47c66bde27b9380a5e72"

KEY_PREFIX = "3L3-"
FORMAT_VERSION = 1

APP_DIR = Path(os.getenv("APPDATA") or Path.home()) / "InterviewAssistant"
LICENSE_FILE = APP_DIR / "license.json"


# ── Server location + HTTP ────────────────────────────────────────

def _looks_like_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def server_url() -> str:
    env = os.getenv("LICENSE_SERVER_URL", "").strip()
    if _looks_like_url(env):
        return env.rstrip("/")
    candidates: list[Path] = []
    try:
        candidates.append(Path(__file__).resolve().parent / "license_server.txt")
    except Exception:
        pass
    try:
        import sys as _sys
        if getattr(_sys, "frozen", False):
            candidates.append(Path(_sys.executable).resolve().parent / "license_server.txt")
    except Exception:
        pass
    for fp in candidates:
        try:
            if fp.exists():
                for line in fp.read_text().splitlines():
                    txt = line.strip()
                    if _looks_like_url(txt):
                        return txt.rstrip("/")
        except Exception:
            continue
    return "http://127.0.0.1:8000"


def _hidden_subprocess_kwargs() -> dict:
    kw: dict = {}
    try:
        if sys.platform.startswith("win"):
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            kw["startupinfo"] = startupinfo
    except Exception:
        pass
    return kw


def _wmic_output(args: list[str], timeout: int = 20) -> str:
    """wmic with retries — right after boot wmic can be very slow, and a single
    timed-out attempt would produce a different device ID for the same machine."""
    last_exc: Exception = TimeoutError("wmic not attempted")
    for _ in range(3):
        try:
            return subprocess.check_output(
                ["wmic"] + args, stderr=subprocess.DEVNULL,
                text=True, timeout=timeout, **_hidden_subprocess_kwargs())
        except Exception as exc:
            last_exc = exc
            time.sleep(1)
    raise last_exc


def get_hwid() -> str:
    parts: list[str] = []
    try:
        out = _wmic_output(["csproduct", "get", "uuid"])
        for line in out.splitlines():
            s = line.strip()
            if s and "UUID" not in s:
                parts.append("mb:" + s)
                break
    except Exception:
        pass
    try:
        serials: list[str] = []
        try:
            out = _wmic_output(["diskdrive", "where",
                                "mediatype='Fixed hard disk media'",
                                "get", "serialnumber"])
            serials = sorted({ln.strip() for ln in out.splitlines()
                              if ln.strip() and "Serial" not in ln.strip()})
        except Exception:
            serials = []
        if not serials:
            out = _wmic_output(["diskdrive", "get", "serialnumber"])
            serials = sorted({ln.strip() for ln in out.splitlines()
                              if ln.strip() and "Serial" not in ln.strip()})
        if serials:
            parts.append("disk:" + "|".join(serials))
    except Exception:
        pass
    parts.append("host:" + platform.node())
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _post(path: str, payload: dict, timeout: int = 60, attempts: int = 3,
          on_retry=None) -> dict:
    """POST JSON, tolerant of Render free-tier cold starts (~60s wake).
    HTTP errors raise immediately; only network errors/timeouts are retried."""
    import urllib.error

    total = max(1, attempts)
    last_exc: Exception = TimeoutError("no attempt made")
    for attempt in range(1, total + 1):
        try:
            req = urllib.request.Request(
                server_url() + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError:
            raise
        except (TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt < total:
                if on_retry is not None:
                    try:
                        on_retry(attempt, total, exc)
                    except Exception:
                        pass
                time.sleep(2)
    raise last_exc


def _http_detail(exc: Exception) -> str:
    try:
        raw = exc.read().decode(errors="replace")[:500]  # type: ignore[attr-defined]
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("detail"):
                return str(data["detail"])
        except Exception:
            pass
        return raw[:300]
    except Exception:
        return str(exc)


def _friendly_net_error(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"Server rejected the request: {msg}"
    if isinstance(exc, TimeoutError) or "timed out" in msg:
        return ("Server took too long (it sleeps when idle and needs ~60s to wake). "
                "Wait a minute and try again — no charge was made.")
    if isinstance(exc, (ConnectionError, OSError)):
        return f"No connection to license server: {exc}"
    return msg


# ── Local key validation (offline) ────────────────────────────────

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _now_monotonic(cached: dict) -> int:
    """Wall clock, but never earlier than the last seen time — blunts a
    simple clock-rollback of the expiry check."""
    return max(int(time.time()), int(cached.get("last_seen", 0) or 0))


def validate_local(key_str: str, cached: dict | None = None) -> tuple[dict | None, str]:
    """Verify signature + version + expiry offline. Returns (payload, 'ok') or
    (None, reason)."""
    s = (key_str or "").strip()
    if not s.startswith(KEY_PREFIX):
        return None, "not a 3lit3 license key"
    body = s[len(KEY_PREFIX):]
    if body.count(".") != 1:
        return None, "malformed key"
    payload_b64, sig_b64 = body.split(".")
    try:
        payload_raw = _b64d(payload_b64)
        sig = _b64d(sig_b64)
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY_HEX))
        pub.verify(sig, payload_raw)
        data = json.loads(payload_raw.decode())
    except Exception:
        return None, "key signature is invalid"
    if not isinstance(data, dict) or data.get("v") != FORMAT_VERSION:
        return None, "unsupported key version"
    if "kid" not in data or "exp" not in data:
        return None, "key payload is incomplete"
    if int(data["exp"]) < _now_monotonic(cached or {}):
        return None, "license expired"
    return data, "ok"


# ── Cache ─────────────────────────────────────────────────────────

def load_cache() -> dict:
    try:
        return json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(data: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        LICENSE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


# ── Server calls ──────────────────────────────────────────────────

def activate(key: str, device_id: str, on_retry=None) -> tuple[bool, str, int]:
    """Bind the key to this device (once per device; server enforces the cap).
    Returns (ok, message, exp)."""
    import urllib.error
    try:
        resp = _post("/activate", {"key": key, "device_id": device_id},
                     timeout=45, attempts=3, on_retry=on_retry)
    except urllib.error.HTTPError as exc:
        return False, _http_detail(exc) or f"HTTP {exc.code}", 0
    except Exception as exc:
        return False, _friendly_net_error(exc), 0
    if resp.get("ok"):
        return True, "activated", int(resp.get("exp", 0))
    return False, str(resp.get("error", "activation failed")), 0


def recheck(kid: str, device_id: str):
    """Soft revocation check for the watchdog.
    True = ok, False = revoked, None = network problem (keep running)."""
    try:
        resp = _post("/recheck", {"kid": kid, "device_id": device_id},
                     timeout=20, attempts=2)
        return bool(resp.get("ok"))
    except Exception:
        return None


def create_invoice(prev_key: str | None = None, device_id: str = "",
                   on_retry=None) -> tuple[str, str, str]:
    """Create an order; returns (order_id, invoice_url, bot_start).
    Pass the previous key to extend it (renewal) instead of starting over.
    device_id makes the purchase activate THIS device at mint time.
    bot_start is the Telegram deep link ("" when the server has no bot) —
    Buy opens it instead of the raw invoice so the key lands in the chat."""
    payload: dict = {}
    if prev_key:
        payload["prev_key"] = prev_key
    if device_id:
        payload["device_id"] = device_id
    resp = _post("/invoice", payload, timeout=60, attempts=3, on_retry=on_retry)
    if "order_id" not in resp:
        raise RuntimeError(
            f"license server at {server_url()} is running the OLD version "
            f"(no order_id in /invoice). Deploy the new shipping/server or "
            f"point LICENSE_SERVER_URL at a local server.")
    return (str(resp["order_id"]), str(resp["invoice_url"]),
            str(resp.get("bot_start") or ""))


def order_status(order_id: str) -> tuple[str, str | None]:
    """GET /order/<id>/status → (status, key|None). Raises on network error."""
    req = urllib.request.Request(
        server_url() + f"/order/{order_id}/status", method="GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    return str(data.get("status", "unknown")), data.get("key")


# ── Zero-config Telegram (relay mode) ─────────────────────────────
#
# The app ships with no bot token: writes go to the license server
# (POST /tg/send) and its shared bot delivers them to the chat that paid
# for this license. The chat is found server-side — never trusted from
# the client.

def whoami(key: str, on_retry=None) -> dict:
    """POST /whoami → {kid, chat_id, bot_username} for this license key.
    Fetched once at boot in relay mode: fills the delivery chat and wakes
    a sleeping Render instance before the first message is due."""
    return _post("/whoami", {"key": key}, timeout=30, attempts=3,
                 on_retry=on_retry)


def tg_relay(key: str, op: str, text: str = "",
             message_id=None) -> "dict | None":
    """One relayed Telegram write. op: push | edit | delete.
    Returns the server's {"ok": …} dict, None on ANY failure (network,
    4xx/5xx, bot down) — the caller drops the message exactly like a
    failed direct Telegram call."""
    payload: dict = {"key": key, "op": op, "text": (text or "")[:4000]}
    if message_id not in (None, ""):
        try:
            payload["message_id"] = int(message_id)
        except (TypeError, ValueError):
            return None
    try:
        resp = _post("/tg/send", payload, timeout=20, attempts=2)
    except Exception:
        return None
    return resp if isinstance(resp, dict) else None


def link_chat_flow(key: str, on_status=None) -> str:
    """One-tap chat link: open t.me/<bot>?start=link_<nonce>, wait for the
    buyer to tap Start, poll /link/status until the server confirms.
    Returns the chat id, or "" on failure/expiry. Blocking (worker thread);
    on_status(text) streams progress to the UI."""
    import secrets as _secrets
    import webbrowser

    def say(msg: str) -> None:
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    say("Contacting the license server…")
    info = whoami(key)                       # HTTPError/timeout propagate
    bot = str(info.get("bot_username") or "").strip()
    if not bot:
        say("The license server has no delivery bot configured.")
        return ""
    nonce = _secrets.token_urlsafe(9)        # ~12 chars, unguessable
    say("Opening Telegram — tap Start in the bot, then wait…")
    webbrowser.open(f"https://t.me/{bot}?start=link_{nonce}")
    for _ in range(75):                      # 75 × 2s ≈ 2.5 min (cold starts)
        time.sleep(2)
        try:
            row = _post("/link/status", {"key": key, "nonce": nonce},
                        timeout=15, attempts=1)
        except Exception:
            continue                         # Render asleep or a blip — keep polling
        chat = str((row or {}).get("chat_id") or "").strip()
        if chat:
            say(f"Linked to chat {chat}.")
            return chat
    say("No link received — check Telegram and try again.")
    return ""


# ── The paywall ───────────────────────────────────────────────────

def ensure_licensed() -> tuple[str, str, str] | None:
    """Blocking paywall. Returns (key, kid, device_id) iff the key is valid
    and activated on this device; None if the user quits.

    Owns the license logic only — the visible window is license_window.py
    (PyQt6, styled like the app), called in when the fast paths fail."""
    hwid = get_hwid()
    cached = load_cache()

    def _store(key: str, kid: str, exp: int) -> None:
        now = int(time.time())
        save_cache({"key": key, "kid": kid, "exp": exp, "device_id": hwid,
                    "first_seen": cached.get("first_seen", now), "last_seen": now})

    # Fast path: valid key already activated on THIS device — no network at all.
    if cached.get("key") and cached.get("device_id") == hwid:
        payload, _ = validate_local(cached["key"], cached)
        if payload:
            cached["last_seen"] = int(time.time())
            save_cache(cached)
            return cached["key"], str(payload["kid"]), hwid

    startup_msg = "Enter key and Activate, or Buy to get one."

    # Cached key valid but not bound to this device (reinstall / new PC):
    # activate now — needs the server exactly once.
    if cached.get("key"):
        payload, _ = validate_local(cached["key"], cached)
        if payload:
            ok, msg, exp = activate(cached["key"], hwid)
            if ok:
                _store(cached["key"], str(payload["kid"]), exp)
                return cached["key"], str(payload["kid"]), hwid
            startup_msg = msg if "server" not in msg.lower() else \
                f"Device activation needed: {msg}"
        else:
            startup_msg = "Saved key is invalid — paste it again or Buy a new one."

    try:
        from license_window import run_gate
    except Exception as exc:
        print(f"[license] gate window unavailable: {exc}")
        return None
    return run_gate(hwid, startup_msg, str(cached.get("key") or ""), _store)


