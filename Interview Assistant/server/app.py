"""License + key-issuance + Telegram bot server.

Database: PostgreSQL (Supabase) via DATABASE_URL env var.
Telegram: One bot token (TELEGRAM_BOT_TOKEN env var), deep-link flow.
  User clicks t.me/BOT_NAME?start=LICENSE_KEY → bot captures chat_id.

Endpoints:
  POST /invoice, /ipn, /activate, /verify, /issue-key  (unchanged logic, PG backend)
  POST /register-telegram  {license_key, hwid}  → returns deep link URL
  GET  /check-telegram     {license_key, hwid}  → returns connection status
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from crypto_utils import issue_encrypted_key

# ── Config ────────────────────────────────────────────────────────
PRICE = float(os.getenv("PRICE_USD", "9.99"))
CURRENCY = os.getenv("PRICE_CURRENCY", "USD")
PERIOD_DAYS = 30
NOW_API = os.getenv("NOWPAYMENTS_API_KEY", "")
NOW_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
MOCK = os.getenv("MOCK_NOWPAYMENTS", "1") == "1"
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
KEY_ISSUANCE_SECRET = os.getenv("KEY_ISSUANCE_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

app = FastAPI(title="InterviewAssistant Server")

# ── Database ──────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                key TEXT PRIMARY KEY,
                hwid TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                current_period_end BIGINT DEFAULT 0,
                created BIGINT DEFAULT 0,
                email TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                key TEXT,
                status TEXT,
                raw TEXT,
                ts BIGINT
            );
            CREATE TABLE IF NOT EXISTS telegram_connections (
                chat_id TEXT PRIMARY KEY,
                bot_token TEXT NOT NULL,
                user_hwid TEXT DEFAULT '',
                license_key TEXT DEFAULT '',
                connected_at BIGINT DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE
            );
        """)
        cur.close()


@app.on_event("startup")
async def _startup():
    init_db()
    print(f"[server] mode={'MOCK' if MOCK else 'LIVE'} pg={'yes' if DATABASE_URL else 'NO'} "
          f"tg_bot={'yes' if TG_BOT_TOKEN else 'NO'} llm_key={'yes' if LLM_API_KEY else 'NO'}",
          flush=True)


@app.get("/status")
async def status():
    return {"mode": "mock" if MOCK else "live",
            "database": "connected" if DATABASE_URL else "missing",
            "telegram_bot": "configured" if TG_BOT_TOKEN else "missing",
            "llm_key": "configured" if LLM_API_KEY else "missing",
            "key_secret": "configured" if KEY_ISSUANCE_SECRET else "missing",
            "price": PRICE, "currency": CURRENCY, "days": PERIOD_DAYS}


# ── Helpers ───────────────────────────────────────────────────────

def new_key() -> str:
    return "IA-" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24].upper()


def nowpayments_invoice(order_id: str) -> str:
    if not NOW_API or MOCK:
        return f"http://127.0.0.1:8000/pay/MOCK-{order_id} (set NOWPAYMENTS_API_KEY for real link)"
    payload = {
        "price_amount": PRICE, "price_currency": CURRENCY,
        "order_id": order_id, "order_description": "Interview Assistant $9.99/mo, 1 seat",
    }
    for field, env in (("ipn_callback_url", "IPN_CALLBACK_URL"),
                       ("success_url", "SUCCESS_URL"),
                       ("cancel_url", "CANCEL_URL")):
        val = os.getenv(env, "").strip()
        if val:
            payload[field] = val
    req = urllib.request.Request(
        "https://api.nowpayments.io/v1/invoice",
        data=json.dumps(payload).encode(),
        headers={"x-api-key": NOW_API, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise HTTPException(status_code=502,
                            detail=f"NOWPayments rejected invoice (HTTP {exc.code}): {detail}")
    try:
        return body["invoice_url"]
    except KeyError:
        raise HTTPException(status_code=502,
                            detail=f"NOWPayments gave no invoice_url: {json.dumps(body)[:500]}")


def _extend(key: str) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_period_end FROM licenses WHERE key=%s", (key,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO licenses(key,hwid,status,current_period_end,created) VALUES(%s,'', 'active',0,%s)",
                        (key, int(time.time())))
            base = 0
        else:
            base = int(row[0])
        base = max(int(time.time()), base)
        new_end = base + PERIOD_DAYS * 86400
        cur.execute("UPDATE licenses SET status='active', current_period_end=%s WHERE key=%s", (new_end, key))
        cur.close()
    return new_end


def _check(key: str, hwid: str) -> tuple[bool, str, int]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT hwid, status, current_period_end FROM licenses WHERE key=%s", (key,))
        row = cur.fetchone()
        cur.close()
    if not row:
        return False, "unknown license key", 0
    bound, status, end = row
    if bound and bound != hwid:
        return False, "key already bound to another device (1 seat)", 0
    if status != "active" or int(end) < time.time():
        return False, "subscription inactive or expired — pay $9.99 to renew", 0
    return True, "ok", int(end)


# ── License endpoints ─────────────────────────────────────────────

@app.post("/invoice")
async def invoice(body: dict):
    hwid = (body.get("hwid") or "")[:64]
    key = (body.get("key") or "").strip() or new_key()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key FROM licenses WHERE key=%s", (key,))
        if not cur.fetchone():
            cur.execute("INSERT INTO licenses(key,hwid,status,current_period_end,created) VALUES(%s,%s, 'pending',0,%s)",
                        (key, hwid, int(time.time())))
        cur.close()
    return {"key": key, "invoice_url": nowpayments_invoice(key),
            "price": PRICE, "currency": CURRENCY, "days": PERIOD_DAYS}


@app.post("/admin/extend")
async def admin_extend(body: dict):
    token = os.getenv("ADMIN_TOKEN", "")
    if not token or not secrets.compare_digest(str(body.get("admin_token", "")), token):
        raise HTTPException(401, "bad admin token")
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "key required")
    days = int(body.get("days", PERIOD_DAYS))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_period_end FROM licenses WHERE key=%s", (key,))
        row = cur.fetchone()
        created = row is None
        if not row:
            cur.execute("INSERT INTO licenses(key,hwid,status,current_period_end,created) VALUES(%s,'', 'active',0,%s)",
                        (key, int(time.time())))
            base = 0
        else:
            base = int(row[0])
        new_end = max(int(time.time()), base) + days * 86400
        cur.execute("UPDATE licenses SET status='active', current_period_end=%s WHERE key=%s", (new_end, key))
        cur.close()
    return {"ok": True, "exp": new_end, "created": created}


@app.post("/admin/lookup")
async def admin_lookup(body: dict):
    token = os.getenv("ADMIN_TOKEN", "")
    if not token or not secrets.compare_digest(str(body.get("admin_token", "")), token):
        raise HTTPException(401, "bad admin token")
    key = (body.get("key") or "").strip()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, current_period_end, hwid FROM licenses WHERE key=%s", (key,))
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM licenses")
        n = cur.fetchone()[0]
        cur.close()
    if not row:
        return {"exists": False, "total_keys": n}
    status, end, hwid = row
    return {"exists": True, "status": status, "exp": int(end),
            "active_now": status == "active" and int(end) > int(time.time()),
            "bound": bool(hwid), "total_keys": n}


@app.post("/activate")
async def activate(body: dict):
    key, hwid = (body.get("key") or "").strip(), (body.get("hwid") or "")[:64]
    if not key or not hwid:
        raise HTTPException(400, "key + hwid required")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT hwid FROM licenses WHERE key=%s", (key,))
        row = cur.fetchone()
        if not row:
            cur.close()
            raise HTTPException(404, "unknown key")
        if not row[0]:
            cur.execute("UPDATE licenses SET hwid=%s WHERE key=%s", (hwid, key))
        cur.close()
    ok, msg, end = _check(key, hwid)
    return {"ok": ok, "error": None if ok else msg, "exp": end}


@app.post("/verify")
async def verify(body: dict):
    return await activate(body)


@app.post("/issue-key")
async def issue_key(body: dict):
    key = (body.get("key") or "").strip()
    hwid = (body.get("hwid") or "").strip()
    if not key or not hwid:
        raise HTTPException(400, "key + hwid required")
    if not LLM_API_KEY:
        raise HTTPException(500, "LLM_API_KEY not configured on server")
    if not KEY_ISSUANCE_SECRET:
        raise HTTPException(500, "KEY_ISSUANCE_SECRET not configured on server")
    ok, msg, _ = _check(key, hwid)
    if not ok:
        raise HTTPException(403, f"license invalid: {msg}")
    try:
        result = issue_encrypted_key(hwid, LLM_API_KEY)
    except Exception as exc:
        raise HTTPException(500, f"key issuance failed: {exc}")
    return {"ok": True, **result}


@app.post("/ipn")
async def ipn(req: Request, x_nowpayments_sig: str = Header(default="", alias="x-nowpayments-sig")):
    raw = await req.body()
    if NOW_IPN_SECRET:
        try:
            data = json.loads(raw)
            sig = hmac.new(NOW_IPN_SECRET.encode(),
                           json.dumps(data, separators=(",", ":")).encode(),
                           hashlib.sha512).hexdigest()
            if not hmac.compare_digest(sig, x_nowpayments_sig):
                raise HTTPException(401, "bad IPN signature")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "bad IPN signature")
    data = json.loads(raw or b"{}")
    order_id = str(data.get("order_id", ""))
    status = str(data.get("payment_status", ""))
    pid = str(data.get("payment_id", ""))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO payments(payment_id,key,status,raw,ts) VALUES(%s,%s,%s,%s,%s) ON CONFLICT (payment_id) DO NOTHING",
                    (pid or order_id + str(time.time()), order_id, status, raw.decode()[:4000], int(time.time())))
        cur.close()
    if status in ("finished", "confirmed") and order_id:
        _extend(order_id)
    return JSONResponse({"ok": True})


@app.post("/simulate_pay")
async def simulate_pay(body: dict):
    if not MOCK:
        raise HTTPException(403, "disabled when MOCK_NOWPAYMENTS=0")
    return {"ok": True, "exp": _extend((body.get("key") or "").strip())}


# ── Telegram deep-link ───────────────────────────────────────────

@app.post("/register-telegram")
async def register_telegram(body: dict):
    """Client calls this to get a deep-link URL for Telegram connection."""
    license_key = (body.get("license_key") or "").strip()
    hwid = (body.get("hwid") or "").strip()
    if not license_key or not hwid:
        raise HTTPException(400, "license_key + hwid required")
    if not TG_BOT_TOKEN:
        raise HTTPException(500, "TELEGRAM_BOT_TOKEN not configured on server")

    ok, msg, _ = _check(license_key, hwid)
    if not ok:
        raise HTTPException(403, f"license invalid: {msg}")

    # Get bot username from Telegram API
    bot_username = _get_bot_username()
    if not bot_username:
        raise HTTPException(500, "Could not fetch bot username from Telegram")

    deep_link = f"https://t.me/{bot_username}?start={license_key}"
    return {"ok": True, "deep_link": deep_link, "bot_username": bot_username,
            "bot_token": TG_BOT_TOKEN}


@app.get("/check-telegram")
async def check_telegram(license_key: str, hwid: str):
    """Client polls this to check if Telegram connection is established."""
    if not license_key or not hwid:
        raise HTTPException(400, "license_key + hwid required")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT chat_id, connected_at FROM telegram_connections "
            "WHERE license_key=%s AND user_hwid=%s AND is_active=TRUE",
            (license_key, hwid))
        row = cur.fetchone()
        cur.close()

    if row:
        return {"connected": True, "chat_id": row[0], "connected_at": row[1]}
    return {"connected": False}


def _get_bot_username() -> str:
    """Fetch bot username from Telegram Bot API."""
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getMe",
            method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        return data.get("result", {}).get("username", "")
    except Exception:
        return ""


# ── Telegram bot (long-polling) ──────────────────────────────────

_tg_offset = 0
_tg_last_poll = 0


def _poll_telegram():
    """Poll Telegram for /start messages. Called from background task."""
    global _tg_offset, _tg_last_poll
    if not TG_BOT_TOKEN:
        return

    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates?offset={_tg_offset}&timeout=5&allowed_updates=[\"message\"]"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return

    for update in data.get("result", []):
        _tg_offset = update["update_id"] + 1
        msg = update.get("message", {})
        text = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if text.startswith("/start ") and chat_id:
            license_key = text[len("/start "):].strip()
            _store_telegram_connection(license_key, chat_id)


def _store_telegram_connection(license_key: str, chat_id: str):
    """Store Telegram chat_id linked to a license key."""
    if not license_key or not chat_id:
        return
    try:
        with get_db() as conn:
            cur = conn.cursor()
            # Find hwid for this license key
            cur.execute("SELECT hwid FROM licenses WHERE key=%s", (license_key,))
            row = cur.fetchone()
            hwid = row[0] if row else ""

            cur.execute("""
                INSERT INTO telegram_connections(chat_id, bot_token, user_hwid, license_key, connected_at, is_active)
                VALUES(%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (chat_id) DO UPDATE SET
                    bot_token=EXCLUDED.bot_token,
                    user_hwid=EXCLUDED.user_hwid,
                    license_key=EXCLUDED.license_key,
                    connected_at=EXCLUDED.connected_at,
                    is_active=TRUE
            """, (chat_id, TG_BOT_TOKEN, hwid, license_key, int(time.time())))
            cur.close()
    except Exception as exc:
        print(f"[telegram] store failed: {exc}", flush=True)


import threading


def _telegram_poll_loop():
    """Background thread: poll Telegram for /start messages."""
    global _tg_last_poll
    while True:
        try:
            _poll_telegram()
        except Exception:
            pass
        time.sleep(3)


@app.on_event("startup")
async def _start_telegram_poll():
    if TG_BOT_TOKEN:
        t = threading.Thread(target=_telegram_poll_loop, daemon=True)
        t.start()
        print("[server] Telegram bot polling started", flush=True)
