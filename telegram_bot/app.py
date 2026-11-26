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

BOT_SHORT_DESCRIPTION = "Setup help and downloads for the Interview Assistant by 3lit3."

BOT_DESCRIPTION = (
    "A local, real-time interview answer assistant: whisper.cpp transcription, "
    "OpenRouter answers, delivered to Telegram.\n"
    "Use /help for setup, configuration and FAQs, and /download to get the ZIP."
)

# Telegram shows this menu when the user taps the "Commands" button or types "/".
BOT_COMMANDS = [
    {"command": "start", "description": "Welcome message and command list"},
    {"command": "help", "description": "Setup, configuration and FAQs"},
    {"command": "download", "description": "Get the Interview Assistant ZIP"},
]

# ── Help content ──────────────────────────────────────────────────────────────
# Each page is a standalone Telegram message (HTML parse mode). Keep every
# page well under Telegram's 4096-character message limit.

HELP_PAGES: dict[str, str] = {
    "start": (
        "<b>👋 Welcome to Interview Assistant</b>\n\n"
        "A local, real-time interview answer assistant by 3lit3. It captures audio, "
        "transcribes with whisper.cpp, asks OpenRouter GPT-4.1-mini, and sends the "
        "answer straight to Telegram.\n\n"
        "<b>Commands</b>\n"
        "/start — this welcome message\n"
        "/help — setup, configuration and FAQs\n"
        "/download — download the ZIP\n\n"
        "Tap a button below to begin."
    ),
    "home": (
        "<b>📘 Interview Assistant — Help</b>\n\n"
        "Pick a topic. Each page is short so it stays readable on a phone.\n\n"
        "<b>Commands</b>\n"
        "/help — reopen this menu\n"
        "/download — get the ZIP\n\n"
        "Tip: you can also tap <b>Download ZIP</b> below."
    ),
    "setup": (
        "<b>1 · Quick start (recommended variant)</b>\n\n"
        "1. Download the ZIP and extract it.\n"
        "2. Open a terminal in <code>Interview Assistant - whispercpp</code>.\n"
        "3. <code>python -m venv win_env</code>, then activate it "
        "(<code>.\\win_env\\Scripts\\Activate.ps1</code> on Windows).\n"
        "4. <code>pip install -r requirements_whispercpp.txt</code>\n"
        "5. Copy <code>.env.example</code> to <code>.env</code> and fill in your "
        "keys (next page).\n"
        "6. <code>python main.py --list-devices</code>, then "
        "<code>python main.py --device &lt;INDEX&gt;</code>.\n\n"
        "The stable <code>faster-whisper</code> build lives in "
        "<code>Interview Assistant/</code> with its own requirements file."
    ),
    "install": (
        "<b>2 · Install &amp; audio routing</b>\n\n"
        "<b>whisper.cpp variant (active)</b>\n"
        "<code>pip install -r requirements_whispercpp.txt</code>\n\n"
        "<b>Stable faster-whisper variant</b>\n"
        "<code>pip install -r requirements.txt</code> inside "
        "<code>Interview Assistant/</code>.\n\n"
        "Check devices: <code>python main.py --list-devices</code>\n"
        "Test routing: <code>python main.py --test-capture --device &lt;INDEX&gt;</code> "
        "(records 5s, no STT/LLM/Telegram).\n\n"
        "For system audio, choose a <b>Stereo Mix</b>, <b>What U Hear</b> or "
        "<b>VB-Audio Virtual Cable</b> input and route playback into it. The first "
        "STT run downloads model weights."
    ),
    "req": (
        "<b>3 · Configuration</b>\n\n"
        "Create <code>.env</code> inside the variant folder:\n"
        "<pre>OPENROUTER_API_KEY=...\n"
        "TELEGRAM_BOT_TOKEN=...\n"
        "TELEGRAM_CHAT_ID=...</pre>\n"
        "Optional: <code>LLM_MODEL</code>, <code>STT_MODEL</code>, "
        "<code>SILENCE_MS</code>, <code>CONTEXT_WINDOW</code>.\n\n"
        "Common flags: <code>--mic</code>, <code>--loopback</code>, "
        "<code>--device &lt;INDEX&gt;</code>, <code>--cv &lt;PATH&gt;</code>, "
        "<code>--chat-id &lt;ID&gt;</code>.\n\n"
        "Python 3.10+ is recommended; no GPU is required."
    ),
    "faq": (
        "<b>❓ FAQs</b>\n\n"
        "<b>1.</b> Do I need a paid API key?\n"
        "<b>2.</b> Which variant should I use?\n"
        "<b>3.</b> How do I capture the interviewer's audio?\n"
        "<b>4.</b> No answers arriving in Telegram\n"
        "<b>5.</b> First run is slow / model download\n"
        "<b>6.</b> Runtime commands + separate bot tokens\n\n"
        "Tap a number to read the answer."
    ),
    "faq:1": (
        "<b>FAQ 1 · Do I need a paid API key?</b>\n\n"
        "LLM calls go through OpenRouter using <code>openai/gpt-4.1-mini</code> by "
        "default, so an OpenRouter API key is required. Check OpenRouter's current "
        "free credit and pricing. Speech-to-text runs locally; only the question and "
        "CV context are sent to the LLM. This bot never asks for your keys."
    ),
    "faq:2": (
        "<b>FAQ 2 · Which variant should I use?</b>\n\n"
        "<code>Interview Assistant - whispercpp</code> is the actively developed "
        "variant: lower latency, <code>--test-capture</code>, and live CV reload over "
        "Telegram. <code>Interview Assistant/</code> is the stable "
        "<code>faster-whisper</code> baseline. Start with whispercpp."
    ),
    "faq:3": (
        "<b>FAQ 3 · How do I capture the interviewer's audio?</b>\n\n"
        "Run <code>python main.py --list-devices</code>. For microphone mode use "
        "<code>--mic</code>. For system audio use <code>--loopback</code> and pick a "
        "Stereo Mix / What U Hear / VB-Audio Cable input; route the meeting's playback "
        "into that cable. Verify with "
        "<code>--test-capture --device &lt;INDEX&gt;</code>."
    ),
    "faq:4": (
        "<b>FAQ 4 · No answers arriving in Telegram</b>\n\n"
        "Check <code>TELEGRAM_BOT_TOKEN</code> and <code>TELEGRAM_CHAT_ID</code> in "
        "<code>.env</code> (or pass <code>--chat-id</code>). Send the bot a message "
        "first, then restart the assistant. Answers are sent to that chat; in mic "
        "mode the transcript is echoed too."
    ),
    "faq:5": (
        "<b>FAQ 5 · First run is slow / model download</b>\n\n"
        "Normal. whisper.cpp / faster-whisper downloads its model weights on first "
        "use. The default is <code>tiny.en</code>; change <code>STT_MODEL</code> in "
        "<code>.env</code> or pass <code>--stt-model</code>. Later runs reuse the "
        "cached model."
    ),
    "faq:6": (
        "<b>FAQ 6 · Runtime commands and bot tokens</b>\n\n"
        "While the assistant runs, message its bot: <code>/cv &lt;path&gt;</code> "
        "reloads a CV, <code>/status</code> shows state, <code>/reset</code> clears "
        "context; sending a .txt/.pdf/.docx document reloads the CV live.\n\n"
        "<b>Important:</b> this help bot needs its <b>own token</b>. Telegram does "
        "not allow a webhook and getUpdates polling on the same token — reusing the "
        "desktop app's token would stop its commands working."
    ),
    "download": (
        "<b>⬇️ Download Interview Assistant</b>\n\n"
        "Tap the button below to get the ZIP.\n\n"
        "If the link does not work, ask the admin to check the bot's "
        "<code>DOWNLOAD_URL</code> setting."
    ),
}

FAQ_COUNT = 6
NEXT_PAGE = {"setup": "install", "install": "req", "req": "faq", "faq": "home"}
PREV_PAGE = {"install": "setup", "req": "install", "faq": "req"}

PAGE_LABELS = {
    "home": "Help home",
    "setup": "Setup",
    "install": "Installation",
    "req": "Requirements",
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
            [_button("📘 Setup", "h:setup"), _button("⚙️ Installation", "h:install")],
            [_button("✅ Requirements", "h:req"), _button("❓ FAQs", "h:faq")],
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

    # Linear guide: setup → install → requirements → FAQs.
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
