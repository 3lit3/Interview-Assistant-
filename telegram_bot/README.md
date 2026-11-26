# Interview Assistant — Telegram Help Bot

A small Telegram bot that answers installation and usage questions for the two Interview Assistant variants in this repo, and sends users the ZIP download link.

| Command | What it does |
|---|---|
| `/start` | Welcome message + command list, with buttons for Quick start, Install, Configuration, FAQs and Download |
| `/help` | Opens the same paged help menu |
| `/download` | Sends a button linking to the Interview Assistant ZIP |
| `/faq` | Jumps straight to the FAQ index |

The command menu and bot "About" text are registered automatically with the Bot API on every service start. This bot is **additive** — it does not modify either existing project folder.

---

## ⚠️ Use a separate Telegram token

The desktop app uses `getUpdates` polling to receive `/cv`, `/status` and `/reset` while it runs. Telegram does **not** allow a webhook and `getUpdates` on the same token.

> Create a **second bot** with @BotFather and put that second token in this service's `TELEGRAM_BOT_TOKEN`. Do **not** reuse the token from either variant's `.env`. Reusing it would silently break the desktop app's runtime commands.

---

## 1. Create the bot

1. In Telegram, message **@BotFather**.
2. Send `/newbot`, choose a name and a username (must end in `bot`).
3. Copy the token — that is this service's `TELEGRAM_BOT_TOKEN`.
4. Optional: `/setdescription` and `/setshortdescription` on BotFather. The service also sets these through the API on boot.
5. Optional: `/setcommands` with:

```
start - Welcome message and command list
help - Setup, configuration and FAQs
download - Get the Interview Assistant ZIP
```

If you skip step 5, `setMyCommands` registers the same menu for you.

---

## 2. Deploy to Render

1. **New → Web Service** and connect this repository.
2. Set **Root Directory** to `telegram_bot`.
3. Runtime: **Python 3.11+**.
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
6. Health check path: `/health`
7. Add environment variables:

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | The **second** bot token, not the app's token |
| `TELEGRAM_WEBHOOK_SECRET` | Recommended | Random string, e.g. `python -c "import secrets;print(secrets.token_urlsafe(24))"` |
| `PUBLIC_URL` | Optional | Render's `RENDER_EXTERNAL_URL` is detected automatically; set this only for a custom domain |
| `DOWNLOAD_URL` | Recommended | Direct HTTPS link to the ZIP |

`render.yaml` in this folder documents the same settings if you prefer a Blueprint. After deploy, open `https://<your-service>.onrender.com/health` — you should see `"bot_configured": true` and a `webhook_url`. Then message the new bot `/start`.

> Free Render instances sleep after inactivity. Telegram webhooks wake the service; the first reply may take a few seconds.

---

## 3. Where to put the ZIP

Pick one:

1. **GitHub Release (recommended):** attach the ZIP as a release asset and paste its direct download URL into `DOWNLOAD_URL`.
2. **Render Static Site:** publish a folder containing `Interview-Assistant.zip` and use its public URL.
3. **This web service:** add the ZIP to `telegram_bot/downloads/` and leave `DOWNLOAD_URL` empty. `GET /download` serves it. Render's filesystem is ephemeral, so the ZIP must be part of the repository/build, not uploaded at runtime.

`DOWNLOAD_FILE` can point to one exact file path instead of auto-detecting `downloads/*.zip`.

---

## 4. How the paged help works

`/help` sends one short menu with inline buttons:

```
📘 Quick start       ⚙️ Installation
✅ Configuration     ❓ FAQs
⬇️ Download ZIP
```

- **Quick start**, **Installation** and **Configuration** are separate short pages with **◀ Back / Next ▶** navigation, so no message hits Telegram's 4096-character limit.
- **FAQs** has six numbered buttons. Each answer has **◀ Previous / Next ▶**, **All FAQs** and **« Help home** buttons.
- Button presses edit the existing message with `editMessageText` instead of posting new ones, keeping the chat clean.
- Every callback is answered (`answerCallbackQuery`) so the loading spinner stops.

FAQ topics: API key / OpenRouter pricing, which variant to use, microphone vs system-audio capture, missing Telegram replies, first-run model download, and runtime commands plus the separate-token warning.

To change wording or add FAQs, edit `HELP_PAGES`, `FAQ_COUNT` and `keyboard_for()` in `app.py`.

---

## 5. Local testing

```powershell
cd telegram_bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Telegram needs a public HTTPS URL. Use a tunnel such as ngrok:

```powershell
ngrok http 8000
```

Set `PUBLIC_URL` to the `https://...ngrok-free.app` URL, then start the service:

```powershell
$env:TELEGRAM_BOT_TOKEN = "your-second-bot-token"
$env:TELEGRAM_WEBHOOK_SECRET = "a-long-random-string"
$env:PUBLIC_URL = "https://your-tunnel.ngrok-free.app"
uvicorn app:app --reload --port 8000
```

Each startup re-registers the webhook at `<PUBLIC_URL>/telegram/webhook`, plus the command menu and bot description.
