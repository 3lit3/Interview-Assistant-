"""Client-side licensing + key issuance: $9.99/mo subscription, 1 seat per key.

Security model (read before modifying):
  - Client NEVER decides payment. Server (server/app.py) is source of truth.
  - App starts pipeline ONLY if server POST /verify {key, hwid} -> {ok:true}.
  - HWID binds key to 1 device. Different HWID -> server rejects.
  - POST /issue-key returns AES-256-CBC encrypted LLM key blob.
  - Client decrypts blob locally (client/crypto_utils.py), uses key in memory.
  - Key is NEVER written to disk. Held in cfg.llm_api_key for the session.

No new deps: stdlib only (urllib, tkinter).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path

PLAN_PRICE = "9.99"
PLAN_INTERVAL = "month"
GRACE_OFFLINE_HOURS = 48
REVERIFY_MINUTES = 30

APP_DIR = Path(os.getenv("APPDATA") or Path.home()) / "InterviewAssistant"
LICENSE_FILE = APP_DIR / "license.json"


def _looks_like_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def server_url() -> str:
    env = os.getenv("LICENSE_SERVER_URL", "").strip()
    if _looks_like_url(env):
        return env.rstrip("/")
    try:
        fp = Path(__file__).resolve().parent / "license_server.txt"
        if fp.exists():
            for line in fp.read_text().splitlines():
                txt = line.strip()
                if _looks_like_url(txt):
                    return txt.rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:8000"


def get_hwid() -> str:
    parts: list[str] = []
    try:
        out = subprocess.check_output(
            ["wmic", "csproduct", "get", "uuid"], stderr=subprocess.DEVNULL,
            text=True, timeout=10,
        )
        for line in out.splitlines():
            s = line.strip()
            if s and "UUID" not in s:
                parts.append("mb:" + s)
                break
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["wmic", "diskdrive", "get", "serialnumber"], stderr=subprocess.DEVNULL,
            text=True, timeout=10,
        )
        for line in out.splitlines():
            s = line.strip()
            if s and "Serial" not in s:
                parts.append("disk:" + s)
                break
    except Exception:
        pass
    parts.append("host:" + platform.node())
    try:
        import uuid as _uuid
        parts.append("mac:" + str(_uuid.getnode()))
    except Exception:
        pass
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _post(path: str, payload: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        server_url() + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_cache() -> dict:
    try:
        return json.loads(LICENSE_FILE.read_text())
    except Exception:
        return {}


def save_cache(data: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(json.dumps(data))


def verify_with_server(key: str, hwid: str) -> tuple[bool, str, int]:
    """Returns (ok, message, exp_epoch)."""
    import urllib.error

    try:
        resp = _post("/verify", {"key": key, "hwid": hwid})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, ("Key not found on server (free-tier restart likely wiped test data). "
                           "Do NOT pay again — contact support with your key."), 0
        return False, f"License server error HTTP {exc.code}", 0
    except Exception as exc:
        c = load_cache()
        if c.get("key") == key and c.get("exp", 0) > time.time():
            left_h = (c["exp"] - time.time()) / 3600
            if left_h > 0 and (c.get("offline_since", 0) == 0 or
                               time.time() - c["offline_since"] < GRACE_OFFLINE_HOURS * 3600):
                c["offline_since"] = c.get("offline_since") or time.time()
                save_cache(c)
                return True, f"Offline grace ({left_h:.1f}h left)", c["exp"]
        return False, f"No connection to license server: {exc}", 0
    if resp.get("ok"):
        c = {"key": key, "exp": int(resp.get("exp", 0)), "offline_since": 0}
        save_cache(c)
        return True, "Subscription active", c["exp"]
    return False, str(resp.get("error", "License invalid")), 0


def create_invoice(hwid: str) -> tuple[str, str]:
    resp = _post("/invoice", {"hwid": hwid, "plan": "monthly_9_99"})
    return str(resp["key"]), str(resp["invoice_url"])


def issue_llm_key(key: str, hwid: str) -> str:
    """Call /issue-key on server, decrypt blob, return plaintext API key.

    Called once at every startup after license verification.
    Returns the LLM API key string. Raises on any failure.
    """
    import urllib.error
    from crypto_utils import decrypt_api_key

    resp = _post("/issue-key", {"key": key, "hwid": hwid}, timeout=20)

    blob = resp.get("blob", "")
    verification = resp.get("verification", "")
    if not blob or not verification:
        raise RuntimeError("server returned empty key issuance response")

    api_key = decrypt_api_key(hwid, blob, verification)
    if not api_key:
        raise RuntimeError("decrypted key is empty")

    return api_key


def ensure_licensed() -> tuple[str, str] | None:
    """Blocking paywall. Returns (key, hwid) iff server verifies active sub."""
    import tkinter as tk
    from tkinter import ttk

    hwid = get_hwid()
    cached = load_cache()
    if cached.get("key"):
        ok, msg, _ = verify_with_server(cached["key"], hwid)
        if ok:
            return cached["key"], hwid

    result: dict = {}
    win = tk.Tk()
    win.title("Interview Assistant — Subscription $9.99/mo")
    win.geometry("480x380")
    win.attributes("-topmost", True)

    ttk.Label(win, text="Interview Assistant", font=("Segoe UI", 14, "bold")).pack(pady=(12, 2))
    ttk.Label(win, text="$9.99 / month · 1 seat · locked to this device").pack()
    ttk.Label(win, text=f"Device ID: {hwid}", font=("Consolas", 8)).pack(pady=6)

    ttk.Label(win, text="License key:").pack()
    key_var = tk.StringVar(value=cached.get("key", ""))
    ttk.Entry(win, textvariable=key_var, width=44).pack(pady=4)

    status = tk.StringVar(value="Enter key and Activate, or Buy to get one.")
    ttk.Label(win, textvariable=status, wraplength=440).pack(pady=6)

    def do_buy():
        status.set("Creating $9.99 invoice…")
        win.update()
        try:
            key, url = create_invoice(hwid)
            key_var.set(key)
            save_cache({"key": key, "exp": 0, "offline_since": 0})
            webbrowser.open(url)
            status.set(f"Key {key} created. Complete payment in browser, then Activate.")
        except Exception as exc:
            status.set(f"Buy failed: {exc} (is license server running?)")

    def do_activate():
        key = key_var.get().strip()
        if not key:
            status.set("Paste your license key first.")
            return
        status.set("Verifying with server…")
        win.update()
        ok, msg, exp = verify_with_server(key, hwid)
        if ok:
            left = (exp - time.time()) / 86400 if exp else 0
            result["key"], result["hwid"] = key, hwid
            status.set(f"Active ({left:.1f} days left). Starting…")
            win.after(600, win.destroy)
        else:
            status.set(f"Not active: {msg}")

    row = ttk.Frame(win)
    row.pack(pady=8)
    ttk.Button(row, text="Buy $9.99/mo", command=do_buy).pack(side="left", padx=6)
    ttk.Button(row, text="Activate", command=do_activate).pack(side="left", padx=6)
    ttk.Button(row, text="Quit", command=win.destroy).pack(side="left", padx=6)

    ttk.Label(win, text="Payment via NOWPayments (crypto). Renewal extends 30 days.\n"
                        "Key works on THIS device only.", wraplength=440,
              font=("Segoe UI", 8)).pack(side="bottom", pady=10)
    win.mainloop()
    if "key" in result:
        return result["key"], result["hwid"]
    return None
