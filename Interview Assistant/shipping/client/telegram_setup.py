"""Telegram deep-link connection: one bot, automatic chat ID capture.

Flow:
  1. Client calls POST /register-telegram {license_key, hwid}
  2. Server returns deep link: t.me/BOT_NAME?start=LICENSE_KEY
  3. User clicks link → Telegram opens → presses Start
  4. Bot receives /start LICENSE_KEY → stores chat_id in DB
  5. Client polls GET /check-telegram → shows "Connected" in UI

No manual token/ID entry needed.
"""
from __future__ import annotations

import json
import os
import tkinter as tk
import urllib.request
from tkinter import ttk
from pathlib import Path

APP_DIR = Path(os.getenv("APPDATA") or Path.home()) / "InterviewAssistant"
USER_ENV = APP_DIR / "user.env"


def load_user_env() -> dict:
    """Load per-user keys into os.environ."""
    found: dict[str, str] = {}
    try:
        for line in USER_ENV.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            found[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k, v in found.items():
        os.environ[k] = v
    return found


def register_telegram(server_url: str, license_key: str, hwid: str) -> tuple[bool, str, str]:
    """Get deep link URL from server. Returns (ok, deep_link_or_error, bot_token)."""
    try:
        req = urllib.request.Request(
            f"{server_url}/register-telegram",
            data=json.dumps({"license_key": license_key, "hwid": hwid}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        if data.get("ok"):
            return True, data["deep_link"], data.get("bot_token", "")
        return False, data.get("error", "Unknown error"), ""
    except Exception as exc:
        return False, f"Server error: {exc}", ""


def save_user_env(bot_token: str, chat_id: str) -> None:
    """Save bot token + chat_id to user.env."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not chat_id.strip():
        try:
            USER_ENV.unlink()
        except OSError:
            pass
        return
    lines = []
    if bot_token.strip():
        lines.append(f"TELEGRAM_BOT_TOKEN={bot_token.strip()}")
    lines.append(f"TELEGRAM_CHAT_ID={chat_id.strip()}")
    USER_ENV.write_text("\n".join(lines) + "\n")


def register_telegram(server_url: str, license_key: str, hwid: str) -> tuple[bool, str, str]:
    """Get deep link URL from server. Returns (ok, deep_link_or_error, bot_token)."""
    try:
        req = urllib.request.Request(
            f"{server_url}/register-telegram",
            data=json.dumps({"license_key": license_key, "hwid": hwid}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        if data.get("ok"):
            return True, data["deep_link"], data.get("bot_token", "")
        return False, data.get("error", "Unknown error"), ""
    except Exception as exc:
        return False, f"Server error: {exc}", ""


def check_telegram(server_url: str, license_key: str, hwid: str) -> dict:
    """Poll server for Telegram connection status."""
    try:
        sep = "&" if "?" in f"{server_url}/check-telegram" else "?"
        url = f"{server_url}/check-telegram{sep}license_key={license_key}&hwid={hwid}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {"connected": False}


def prompt_telegram_deep_link(server_url: str, license_key: str, hwid: str) -> tuple[str, str, str]:
    """GUI dialog for Telegram deep-link connection.
    Returns (action, chat_id, bot_token); action is 'connected' or 'skip'.
    """
    result = {"action": "skip", "chat_id": "", "bot_token": ""}

    win = tk.Tk()
    win.title("Connect Telegram")
    win.geometry("520x480")
    win.attributes("-topmost", True)

    ttk.Label(win, text="Connect Telegram", font=("Segoe UI", 14, "bold")).pack(pady=(12, 4))
    ttk.Label(win, text="One-time setup. Answers mirror to your phone during recordings.",
              wraplength=460).pack()

    status = tk.StringVar(value="Click below to get your connection link.")
    ttk.Label(win, textvariable=status, wraplength=460, foreground="gray").pack(pady=8)

    link_var = tk.StringVar(value="")
    link_entry = ttk.Entry(win, textvariable=link_var, width=55, state="readonly")
    link_entry.pack(pady=4)

    stored_token = {"value": ""}

    def do_register():
        status.set("Getting connection link from server...")
        win.update()
        ok, result_text, bot_token = register_telegram(server_url, license_key, hwid)
        if ok:
            link_var.set(result_text)
            stored_token["value"] = bot_token
            status.set("Click the link below, then press Start in Telegram.")
            try:
                import webbrowser
                webbrowser.open(result_text)
            except Exception:
                pass
        else:
            status.set(f"Error: {result_text}")

    def do_check():
        status.set("Checking connection...")
        win.update()
        data = check_telegram(server_url, license_key, hwid)
        if data.get("connected"):
            chat_id = data.get("chat_id", "")
            save_user_env(stored_token["value"], chat_id)
            result["action"] = "connected"
            result["chat_id"] = chat_id
            result["bot_token"] = stored_token["value"]
            status.set(f"Connected! Chat ID: {chat_id}")
            win.after(800, win.destroy)
        else:
            status.set("Not connected yet. Did you press Start in Telegram?")

    ttk.Button(win, text="Get Connection Link", command=do_register).pack(pady=6)

    help_box = tk.Text(win, height=8, wrap="word")
    help_box.pack(fill="both", expand=True, padx=10, pady=6)
    help_box.insert("1.0",
        "How to connect:\n"
        "1. Click 'Get Connection Link' above\n"
        "2. A Telegram chat will open with the bot\n"
        "3. Press Start (or send any message)\n"
        "4. Click 'Check Connection' below\n"
        "5. Done — answers will mirror to this chat"
    )
    help_box.configure(state="disabled")

    ttk.Button(win, text="Check Connection", command=do_check).pack(pady=4)

    def do_skip():
        win.destroy()

    ttk.Button(win, text="Skip for now", command=do_skip).pack(pady=(0, 10))
    win.protocol("WM_DELETE_WINDOW", do_skip)
    win.mainloop()
    return result["action"], result["chat_id"], result["bot_token"]
