"""Telegram bot web service for Interview Assistant setup help + ZIP downloads.

Render-friendly: FastAPI + Telegram webhook, no background poller and no
persistent state. The desktop app is untouched; this bot only answers the
/start, /help and /download commands (and the FAQ buttons under /help).

Use a DIFFERENT BotFather token from the one in the desktop app's .env:
Telegram does not allow a webhook and getUpdates polling on the same token.

Required environment variables:
    TELEGRAM_BOT_TOKEN   token from @BotFather (separate from the app's token)

Recommended on Render:
    TELEGRAM_WEBHOOK_SECRET  random string; also sent to Telegram as secret_token
    PUBLIC_URL               e.g. https://my-bot.onrender.com (Render sets this
                             automatically as RENDER_EXTERNAL_URL)
    DOWNLOAD_URL             direct HTTPS link to the ZIP (or leave empty to
                             serve a file from ./downloads at /download)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("interview-assistant-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_SECRET_RAW = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
PUBLIC_URL = (
    os.getenv("PUBLIC_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
).rstrip("/")
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", "").strip()
DOWNLOAD_FILE = os.getenv("DOWNLOAD_FILE", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR") or (Path(__file__).parent / "downloads"))
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
WEBHOOK_PATH = "/telegram/webhook"


def _telegram_safe_secret(raw: str) -> str:
    """Telegram secret_token allows only 1-256 chars of [A-Za-z0-9_-]."""
    cleaned = "".join(
        ch for ch in raw.strip().strip('"').strip("'")
        if ch.isascii() and (ch.isalnum() or ch in "_-")
    )
    return cleaned[:256]


WEBHOOK_SECRET = _telegram_safe_secret(WEBHOOK_SECRET_RAW)
if WEBHOOK_SECRET_RAW and WEBHOOK_SECRET != WEBHOOK_SECRET_RAW:
    log.warning(
        "TELEGRAM_WEBHOOK_SECRET contained characters Telegram rejects "
        "(+ / = spaces or quotes); sanitized it to a valid secret."
    )

WEBHOOK_REGISTERED = False
WEBHOOK_ERROR: str | None = None
LAST_TELEGRAM_ERROR: str | None = None

# ── What users see when they open the bot / type a slash ──────────────────────

BOT_SHORT_DESCRIPTION = "Setup help, license info and downloads for the 3lit3 Interview Assistant app."

BOT_DESCRIPTION = (
    "Setup help, license info and downloads for the 3lit3 Interview Assistant Windows app.\n"
    "Use /help for installation, first-run setup, interview tips, payment details and FAQs, "
    "and /download for the installer."
)

# Telegram shows this menu when the user taps the "Commands" button or types "/".
BOT_COMMANDS = [
    {"command": "start", "description": "Welcome message and command list"},
    {"command": "help", "description": "Setup, license and FAQs"},
    {"command": "download", "description": "Get the Interview Assistant installer"},
]

# ── Help content ──────────────────────────────────────────────────────────────
# Each page is a standalone Telegram message (HTML parse mode). Keep every
# page well under Telegram's 4096-character message limit.

HELP_PAGES: dict[str, str] = {
    "start": (
        "<b>👋 Welcome to 3lit3 Interview Assistant</b>\n\n"
        "A Windows interview assistant that listens, transcribes and suggests answers "
        "while you interview. This bot gives you setup help, licensing details and "
        "the installer.\n\n"
        "<b>Commands</b>\n"
        "/start — this welcome message\n"
        "/help — installation, first run, usage, payment and FAQs\n"
        "/download — get the installer ZIP\n\n"
        "Tap a button below to begin."
    ),
    "home": (
        "<b>📘 3lit3 Interview Assistant — Help</b>\n\n"
        "Pick a topic. Every page is short so it stays readable on a phone.\n\n"
        "<b>Commands</b>\n"
        "/help — reopen this menu\n"
        "/download — get the installer\n\n"
        "Tip: you can also tap <b>Download ZIP</b> below."
    ),
    "install": (
        "<b>1 · Install</b>\n\n"
        "<b>Requirements</b>\n"
        "• Windows 10/11, 64-bit\n"
        "• <b>No Python needed</b> — the runtime is bundled\n"
        "• Microphone + speakers\n"
        "• Internet + a <b>free Gemini API key</b>\n\n"
        "<b>Steps</b>\n"
        "1. Download and run <code>3lit3_Interview_Assistant.exe</code>.\n"
        "2. Choose whether to add a desktop icon, then click <b>Install</b>.\n"
        "3. No admin/UAC prompt — it installs per user to:\n"
        "<code>%LocalAppData%\\Programs\\3lit3</code>\n"
        "4. Launch it from the Start menu or desktop icon.\n\n"
        "The console window showing the <code>[JARVIS]</code> transcript is intentional."
    ),
    "first_run": (
        "<b>2 · First run</b>\n\n"
        "On first launch you will see:\n"
        "<pre>◈  INITIALISATION REQUIRED\nConfigure J.A.R.V.I.S. before first boot.</pre>\n\n"
        "1. Paste a free Gemini key from Google AI Studio (starts with <code>AIza…</code>).\n"
        "2. Confirm the detected OS, or click Windows / macOS / Linux.\n"
        "3. Click <b>▸ INITIALISE SYSTEMS</b>.\n\n"
        "Settings are saved to:\n"
        "<code>%LocalAppData%\\Programs\\3lit3\\config\\api_keys.json</code>\n\n"
        "If the key is rejected, the setup screen reappears so you can replace it."
    ),
    "use": (
        "<b>3 · Everyday use</b>\n\n"
        "<b>Three ways to speak</b>\n"
        "• Wake word: say <i>“Hey Jarvis”</i> (after enabling it)\n"
        "• Push-to-talk: hold <code>Ctrl+Space</code>\n"
        "• Type into the HUD input box and press Enter or <code>▸</code>\n\n"
        "<b>Shortcuts</b>\n"
        "<code>F4</code> mute/unmute · <code>F11</code> fullscreen · <code>Esc</code> interrupt\n\n"
        "<b>Settings drawers</b>\n"
        "⚙ SETUP: remote control, Telegram, shortcut, auto-start, customise, audio "
        "devices, memory, plugins.\n"
        "🎛 CONTROLS: fullscreen, morning brief, wake word, sleep/wake, push-to-talk, "
        "HUD style.\n\n"
        "Click a header again to close the drawer."
    ),
    "interview": (
        "<b>4 · Interview mode</b>\n\n"
        "1. Under <b>CV UPLOAD</b>, choose your CV (.pdf, .docx, .doc, .txt, .md).\n"
        "2. Under <b>INTERVIEW CONTEXT</b>, type the role/company/topic and click "
        "<code>▸</code>.\n"
        "3. Suggested answers appear in <b>AI SUGGESTIONS &amp; FEEDBACK</b> as text.\n"
        "4. Use <b>✋ INTERRUPT [ESC]</b> or the mic button to go quiet.\n\n"
        "Answers are text-only by default so the interviewer cannot hear them. To hear "
        "them, set <code>\"speak_answers\": true</code> in "
        "<code>config\\api_keys.json</code> and restart.\n\n"
        "For computer audio, switch <b>🔊 SYSTEM SOUND ON</b>. Set Windows' default "
        "output to the stereo <b>Speakers (VB-Audio Virtual Cable)</b> device — not the "
        "16-channel endpoint."
    ),
    "license": (
        "<b>💳 License &amp; payment</b>\n\n"
        "<b>Price:</b> $14.99 for <b>30 days</b>. One key works on <b>up to 2 devices</b>.\n\n"
        "<b>How payment works</b>\n"
        "1. In the app press <b>Buy</b>. It opens a chat with the <b>license bot</b> "
        "and a deep link like <code>/start buy_&lt;order_id&gt;</code>.\n"
        "2. The license bot links your chat to the order and sends an "
        "<b>Open invoice</b> button (NowPayments).\n"
        "3. Pay in the browser. NowPayments confirms by IPN, the server mints a signed "
        "key, and the bot sends it to that chat.\n"
        "4. The message includes a <code>myapp://license?key=...</code> link — tap it "
        "to redeem in the app.\n"
        "5. Send <code>/mykeys</code> to the <b>license bot</b> to see keys tied to "
        "your chat.\n\n"
        "<b>Important:</b> payment commands go to the license bot opened by the app — "
        "not to this help bot. If you already paid, sending "
        "<code>/start buy_&lt;order_id&gt;</code> to the license bot resends your key.\n\n"
        "Activation uses the server once per device; everyday validation is offline, "
        "so a Render restart cannot lock you out."
    ),
    "faq": (
        "<b>❓ FAQs</b>\n\n"
        "<b>1.</b> Do I need Python or admin rights?\n"
        "<b>2.</b> Do I need a paid API key?\n"
        "<b>3.</b> Where is the app installed?\n"
        "<b>4.</b> Mic not detected / no suggestions\n"
        "<b>5.</b> Can I hear answers aloud?\n"
        "<b>6.</b> How do I capture computer audio?\n"
        "<b>7.</b> Wake word not responding\n"
        "<b>8.</b> Price, devices and payment\n\n"
        "Tap a number to read the answer."
    ),
    "faq:1": (
        "<b>FAQ 1 · Python or admin rights?</b>\n\n"
        "No. The installer bundles the Python runtime and installs per user without a "
        "UAC prompt. You only need Python if you build from source. If Windows asks "
        "for admin rights, you are running a different installer."
    ),
    "faq:2": (
        "<b>FAQ 2 · Paid API key?</b>\n\n"
        "No. Create a free Gemini key at "
        "<a href=\"https://aistudio.google.com/app/apikey\">Google AI Studio</a>. It is "
        "saved locally in <code>config\\api_keys.json</code> and is not bundled into "
        "the installer. The license key is separate: $14.99 covers 30 days of app use "
        "on up to 2 devices."
    ),
    "faq:3": (
        "<b>FAQ 3 · Where is the app installed?</b>\n\n"
        "<code>%LocalAppData%\\Programs\\3lit3</code>. Press <code>Win + R</code>, "
        "paste that path and press Enter. Your key, CV, memory and logs live there. "
        "Uninstall deletes <code>config</code>, <code>memory</code> and <code>logs</code>, "
        "so back up <code>api_keys.json</code> first."
    ),
    "faq:4": (
        "<b>FAQ 4 · Mic or suggestions problem</b>\n\n"
        "Choose the input under <b>⚙ → 🎧 AUDIO DEVICES</b>, check Windows microphone "
        "privacy settings, and make sure <code>F4</code> has not muted it. If no "
        "suggestions appear, confirm the CV loaded, add interview context and check "
        "that <code>Esc</code> was not pressed."
    ),
    "faq:5": (
        "<b>FAQ 5 · Hear answers aloud?</b>\n\n"
        "Set <code>\"speak_answers\": true</code> in "
        "<code>config\\api_keys.json</code> and restart. Keep <code>interview_mode</code> "
        "at <code>true</code> for text-only interview mode. Video overlays start muted "
        "— click <b>🔈 SOUND OFF</b> to unmute."
    ),
    "faq:6": (
        "<b>FAQ 6 · Capture computer audio?</b>\n\n"
        "Set Windows' default output to the stereo <b>Speakers (VB-Audio Virtual "
        "Cable)</b> device, not the 16-channel endpoint, then switch "
        "<b>🔊 SYSTEM SOUND ON</b> in the app. The app handles echo and mic muting."
    ),
    "faq:7": (
        "<b>FAQ 7 · Wake word not responding</b>\n\n"
        "Install it from <b>🎛 → ⬇ WAKE WORD: DOWNLOAD</b>, or use "
        "<b>Ctrl+Space</b> or <b>👂 WAKE NOW</b>. It sleeps after about two minutes "
        "of silence by design."
    ),
    "faq:8": (
        "<b>FAQ 8 · Price, devices and payment</b>\n\n"
        "$14.99 gives you 30 days and up to 2 devices per key. Press <b>Buy</b> in the "
        "app to open the license bot in Telegram; it sends a NowPayments invoice. "
        "After payment the key and a <code>myapp://license?key=...</code> link are "
        "delivered to that chat. Use <code>/mykeys</code> there to list your keys. "
        "This help bot does not take payments."
    ),
    "download": (
        "<b>⬇️ Download 3lit3 Interview Assistant</b>\n\n"
        "Tap the button below to get the Windows installer/ZIP.\n\n"
        "If the link does not work, ask the admin to check the bot's "
        "<code>DOWNLOAD_URL</code> setting."
    ),
}

FAQ_COUNT = 8
NEXT_PAGE = {
    "install": "first_run",
    "first_run": "use",
    "use": "interview",
    "interview": "license",
    "license": "faq",
    "faq": "home",
}
PREV_PAGE = {
    "first_run": "install",
    "use": "first_run",
    "interview": "use",
    "license": "interview",
    "faq": "license",
}

PAGE_LABELS = {
    "home": "Help home",
    "install": "Install",
    "first_run": "First run",
    "use": "Everyday use",
    "interview": "Interview mode",
    "license": "License & payment",
    "faq": "FAQs",
    "download": "Download",
}


# ── Telegram Bot API helpers ──────────────────────────────────────────────────


def _api(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One Bot API call. Failures are logged, never raised into the webhook."""
    global LAST_TELEGRAM_ERROR
    if not BOT_TOKEN:
        LAST_TELEGRAM_ERROR = "TELEGRAM_BOT_TOKEN is not set"
        log.warning("TELEGRAM_BOT_TOKEN missing; skipping %s", method)
        return None
    try:
        resp = requests.post(f"{API_BASE}/{method}", json=payload or {}, timeout=10)
    except requests.RequestException as exc:
        LAST_TELEGRAM_ERROR = str(exc)
        log.warning("Telegram %s failed: %s", method, exc)
        return None
    try:
        data = resp.json()
    except ValueError:
        LAST_TELEGRAM_ERROR = f"non-JSON response (HTTP {resp.status_code})"
        log.warning("Telegram %s returned non-JSON (HTTP %s)", method, resp.status_code)
        return None
    if not data.get("ok"):
        LAST_TELEGRAM_ERROR = str(data.get("description") or data)
        log.warning("Telegram %s error: %s", method, LAST_TELEGRAM_ERROR)
        return None
    LAST_TELEGRAM_ERROR = None
    return data


def send_message(
    chat_id: int | str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _api("sendMessage", payload)


def edit_message(
    chat_id: int | str,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _api("editMessageText", payload)


def answer_callback(callback_id: str, text: str | None = None) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:200]
    _api("answerCallbackQuery", payload)


# ── Inline keyboards ──────────────────────────────────────────────────────────


def _button(text: str, callback_data: str | None = None, url: str | None = None) -> dict:
    button: dict[str, str] = {"text": text}
    if url:
        button["url"] = url
    else:
        button["callback_data"] = callback_data or "h:home"
    return button


def _keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def download_url() -> str:
    """External link if configured; otherwise this service's /download route."""
    if DOWNLOAD_URL:
        return DOWNLOAD_URL
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/download"
    return ""


def keyboard_for(page: str) -> dict[str, Any]:
    if page in ("start", "home"):
        return _keyboard([
            [_button("💿 Install", "h:install"), _button("🚀 First run", "h:first_run")],
            [_button("🎮 Everyday use", "h:use"), _button("🎤 Interview mode", "h:interview")],
            [_button("💳 License & payment", "h:license"), _button("❓ FAQs", "h:faq")],
            [_button("⬇️ Download ZIP", "h:download")],
        ])

    if page == "faq":
        return _keyboard([
            [_button(str(i), f"h:faq:{i}") for i in range(1, 4)],
            [_button(str(i), f"h:faq:{i}") for i in range(4, FAQ_COUNT + 1)],
            [_button("« Help home", "h:home")],
        ])

    if page.startswith("faq:"):
        i = int(page.split(":")[1])
        row: list[dict[str, str]] = []
        if i > 1:
            row.append(_button("◀ Previous", f"h:faq:{i - 1}"))
        if i < FAQ_COUNT:
            row.append(_button("Next ▶", f"h:faq:{i + 1}"))
        return _keyboard([
            row or [_button("All FAQs", "h:faq")],
            [_button("📋 All FAQs", "h:faq"), _button("« Help home", "h:home")],
        ])

    if page == "download":
        url = download_url()
        rows: list[list[dict[str, str]]] = []
        if url:
            rows.append([_button("⬇️ Download ZIP", url=url)])
        rows.append([_button("« Help home", "h:home")])
        return _keyboard(rows)

    # Linear guide: install → first run → everyday use → interview → license → FAQs.
    row = []
    if page in PREV_PAGE:
        row.append(_button("◀ Back", f"h:{PREV_PAGE[page]}"))
    else:
        row.append(_button("« Help home", "h:home"))
    if page in NEXT_PAGE:
        row.append(_button("Next ▶", f"h:{NEXT_PAGE[page]}"))
    return _keyboard([
        row,
        [_button("⬇️ Download ZIP", "h:download"), _button("❓ FAQs", "h:faq")],
    ])


def page_text(page: str) -> str:
    if page in HELP_PAGES:
        return HELP_PAGES[page]
    return HELP_PAGES["home"]


# ── Command and callback handling ────────────────────────────────────────────


def handle_message(message: dict[str, Any]) -> None:
    sender = message.get("from") or {}
    if sender.get("is_bot"):
        return
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = (message.get("text") or "").strip()
    private_chat = chat.get("type") == "private"
    if not text:
        if private_chat:
            send_message(
                chat_id,
                "I only answer commands right now. Try /help to see what I can do.",
            )
        return

    command = text.split(maxsplit=1)[0].split("@")[0].lower()
    if command == "/start":
        send_message(chat_id, page_text("start"), keyboard_for("start"))
    elif command in ("/help", "/faq"):
        page = "faq" if command == "/faq" else "home"
        send_message(chat_id, page_text(page), keyboard_for(page))
    elif command == "/download":
        send_message(chat_id, page_text("download"), keyboard_for("download"))
    elif command.startswith("/"):
        send_message(
            chat_id,
            "I don't know that command yet. Use /help for the setup guide and FAQs, "
            "or /download to get the ZIP.",
        )
    elif private_chat:
        send_message(
            chat_id,
            "I only answer commands right now. Try /help to see the setup guide "
            "and FAQs, or /download to get the ZIP.",
        )


def handle_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id")
    if not callback_id:
        return
    data = callback.get("data") or ""

    if not data.startswith("h:"):
        answer_callback(callback_id, "Button expired — send /help again.")
        return

    page = data[2:]
    if page not in HELP_PAGES:
        answer_callback(callback_id, "That page is no longer available.")
        return

    msg = callback.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id, message_id = chat.get("id"), msg.get("message_id")
    if chat_id is not None and message_id is not None:
        edit_message(chat_id, int(message_id), page_text(page), keyboard_for(page))
    elif callback.get("inline_message_id"):
        payload = {
            "inline_message_id": callback["inline_message_id"],
            "text": page_text(page)[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": keyboard_for(page),
        }
        _api("editMessageText", payload)
    answer_callback(callback_id)


def configure_bot() -> None:
    """Register the command menu, bot description and webhook on every boot."""
    global WEBHOOK_REGISTERED, WEBHOOK_ERROR, LAST_TELEGRAM_ERROR

    WEBHOOK_REGISTERED = False
    WEBHOOK_ERROR = None

    if not BOT_TOKEN:
        WEBHOOK_ERROR = "TELEGRAM_BOT_TOKEN is not set"
        log.warning("%s — the bot cannot answer yet", WEBHOOK_ERROR)
        return

    _api("setMyCommands", {"commands": BOT_COMMANDS, "language_code": "en"})
    _api("setMyDescription", {"description": BOT_DESCRIPTION, "language_code": "en"})
    _api(
        "setMyShortDescription",
        {"short_description": BOT_SHORT_DESCRIPTION, "language_code": "en"},
    )

    if not PUBLIC_URL:
        WEBHOOK_ERROR = "PUBLIC_URL / RENDER_EXTERNAL_URL is not set"
        log.warning(
            "%s — webhook was not registered. Set PUBLIC_URL to the https:// URL "
            "of this service and restart.", WEBHOOK_ERROR
        )
        return

    payload: dict[str, Any] = {
        "url": f"{PUBLIC_URL}{WEBHOOK_PATH}",
        "allowed_updates": ["message", "callback_query"],
    }
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    LAST_TELEGRAM_ERROR = None
    result = _api("setWebhook", payload)
    if result is not None:
        WEBHOOK_REGISTERED = True
        log.info("Webhook registered at %s%s", PUBLIC_URL, WEBHOOK_PATH)
    else:
        WEBHOOK_ERROR = LAST_TELEGRAM_ERROR or "unknown setWebhook error"
        log.error(
            "Webhook registration FAILED: %s — Telegram will not deliver /start, "
            "/help or /download until this is fixed.", WEBHOOK_ERROR
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_bot()
    yield


app = FastAPI(title="Interview Assistant Telegram Bot", lifespan=lifespan)


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>Interview Assistant Telegram Bot</title>"
        "<h1>Interview Assistant Telegram Bot</h1>"
        "<p>Setup help and ZIP downloads for the interview assistant.</p>"
        "<p><a href='/health'>/health</a> · <a href='/download'>/download</a></p>"
    )


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "bot_configured": bool(BOT_TOKEN),
        "webhook_url": f"{PUBLIC_URL}{WEBHOOK_PATH}" if PUBLIC_URL else None,
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "webhook_registered": WEBHOOK_REGISTERED,
        "webhook_error": WEBHOOK_ERROR,
        "download_url": download_url() or None,
    })


@app.get("/download")
def download_file():
    """Serve a bundled ZIP, or redirect to the configured external download."""
    if DOWNLOAD_URL:
        # Avoid a redirect loop if someone points DOWNLOAD_URL back at this route.
        self_url = f"{PUBLIC_URL}/download" if PUBLIC_URL else ""
        if DOWNLOAD_URL.rstrip("/") != self_url:
            return RedirectResponse(DOWNLOAD_URL)

    candidates: list[Path] = []
    if DOWNLOAD_FILE:
        candidates.append(Path(DOWNLOAD_FILE))
    candidates.extend(sorted(DOWNLOAD_DIR.glob("*.zip")))
    for path in candidates:
        if path.is_file():
            return FileResponse(
                path, filename=path.name, media_type="application/zip"
            )

    return HTMLResponse(
        "<h1>Download not configured</h1>"
        "<p>Set <code>DOWNLOAD_URL</code> to a direct ZIP link, or place a "
        "<code>.zip</code> file in the service's <code>downloads/</code> folder.</p>",
        status_code=404,
    )


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> JSONResponse:
    if WEBHOOK_SECRET:
        received = request.headers.get("x-telegram-bot-api-secret-token", "")
        if received != WEBHOOK_SECRET:
            log.warning("Rejected webhook request with invalid secret token")
            return JSONResponse({"ok": False, "error": "invalid secret"}, status_code=403)

    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(update, dict):
        return JSONResponse({"ok": False, "error": "invalid update"}, status_code=400)

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        handle_callback(callback)
    else:
        message = update.get("message") or update.get("edited_message")
        if isinstance(message, dict):
            try:
                handle_message(message)
            except Exception:
                log.exception("Failed to handle Telegram message")
    # Always answer 200: a retry storm is worse than a single lost reply.
    return JSONResponse({"ok": True})
