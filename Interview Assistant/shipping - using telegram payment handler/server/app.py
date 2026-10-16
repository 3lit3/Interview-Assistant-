"""License server: NowPayments invoice → signed 30-day key → device activation.

Flow
----
  1. Client POST /invoice {prev_key?}      → {order_id, invoice_url}
  2. Buyer pays in the browser. Success page: GET /success/<order_id>
  3. NowPayments POST /ipn (HMAC-verified) → server mints a signed Ed25519 key
     (price $14.99, 30 days; a valid prev_key extends from its expiry).
  4. /success/<order_id> shows the key once issued; the client also polls
     GET /order/<order_id>/status and auto-fills it.
  5. Client POST /activate {key, device_id} binds the key to this device.
     Max DEVICE_CAP distinct devices per key — activation needs the server
     exactly once per device.
  6. Everyday launches validate the key OFFLINE (signature + expiry inside the
     key itself). Render being asleep can never lock out an activated device.
     Optional soft revocation: client POST /recheck {kid, device_id} when
     online; /admin/revoke flips the flag.

Database: PostgreSQL when DATABASE_URL is set (Render/Supabase), otherwise a
local SQLite file (server/licensing.db) so the whole flow is dev-testable.

MOCK_NOWPAYMENTS=1 replaces the real invoice with GET /mock-pay/<order_id>,
which simulates a finished payment end-to-end.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from html import escape

import keysign
import tgpay
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


def _load_dotenv(path: str) -> None:
    """Minimal .env loader for local dev (Render uses dashboard env instead).
    Existing environment variables always win. No dependency needed."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Config ────────────────────────────────────────────────────────
PRICE = float(os.getenv("PRICE_USD", "14.99"))
PERIOD_DAYS = int(os.getenv("PERIOD_DAYS", "30"))
DEVICE_CAP = int(os.getenv("DEVICE_CAP", "2"))
NOW_API = os.getenv("NOWPAYMENTS_API_KEY", "")
NOW_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
MOCK = os.getenv("MOCK_NOWPAYMENTS", "1") == "1"
DATABASE_URL = os.getenv("DATABASE_URL", "")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SIGNING: "keysign.Ed25519PrivateKey | None" = None

app = FastAPI(title="Interview Assistant License Server")


def _require_signing() -> "keysign.Ed25519PrivateKey":
    if SIGNING is None:
        raise HTTPException(500, "SIGNING_PRIVATE_KEY not configured on server")
    return SIGNING


# ── Database (PostgreSQL, or SQLite for local development) ────────

def _is_sqlite() -> bool:
    return (not DATABASE_URL) or DATABASE_URL.startswith("sqlite")


def _sql(q: str) -> str:
    # Queries here use ? placeholders; psycopg2 wants %s.
    return q if _is_sqlite() else q.replace("?", "%s")


@contextmanager
def get_db():
    if _is_sqlite():
        path = DATABASE_URL.split("sqlite:///")[-1] if DATABASE_URL else \
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "licensing.db")
        conn = sqlite3.connect(path)
    else:
        url = DATABASE_URL
        if "sslmode" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        import psycopg2
        conn = psycopg2.connect(url)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    prev_key TEXT DEFAULT '',
    created  BIGINT DEFAULT 0,
    device_id TEXT DEFAULT '',
    chat_id   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS issued_keys (
    kid      TEXT PRIMARY KEY,
    key_str  TEXT NOT NULL,
    order_id TEXT DEFAULT '',
    note     TEXT DEFAULT '',
    iat      BIGINT DEFAULT 0,
    exp      BIGINT DEFAULT 0,
    revoked  INTEGER DEFAULT 0,
    chat_id  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS activations (
    kid          TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    activated_at BIGINT DEFAULT 0,
    PRIMARY KEY (kid, device_id)
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    order_id   TEXT DEFAULT '',
    status     TEXT DEFAULT '',
    raw        TEXT DEFAULT '',
    ts         BIGINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_issued_order ON issued_keys(order_id);
"""


def _column_exists(cur, table: str, name: str) -> bool:
    if _is_sqlite():
        cur.execute(f"PRAGMA table_info({table})")
        return any(r[1] == name for r in cur.fetchall())
    cur.execute("SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name=%s", (table, name))
    return cur.fetchone() is not None


def _ensure_columns(cur, table: str, cols: list) -> None:
    """Tolerant migration: add columns that an older database is missing.
    Never raises — a database that cannot be altered still has to serve."""
    for name, decl in cols:
        if _column_exists(cur, table, name):
            continue
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            print(f"[db] migrated {table}.{name}", flush=True)
        except Exception as exc:
            print(f"[db] could not add {table}.{name}: {exc}", flush=True)


def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        for stmt in _SCHEMA.split(";"):
            if stmt.strip():
                cur.execute(stmt)
        _ensure_columns(cur, "orders",
                        [("device_id", "TEXT DEFAULT ''"), ("chat_id", "TEXT DEFAULT ''")])
        _ensure_columns(cur, "issued_keys", [("chat_id", "TEXT DEFAULT ''")])
        # v1 Postgres already had a payments table without order_id
        _ensure_columns(cur, "payments", [("order_id", "TEXT DEFAULT ''")])
        cur.close()


@app.on_event("startup")
def _startup():
    global SIGNING
    try:
        SIGNING = keysign.load_private_key()
        signing_state = "yes"
    except Exception as exc:
        SIGNING = None
        signing_state = f"NO ({exc})"
    print(f"[server] mode={'MOCK' if MOCK else 'LIVE'} db={'sqlite' if _is_sqlite() else 'pg'} "
          f"signing={signing_state} price={PRICE} days={PERIOD_DAYS} cap={DEVICE_CAP}", flush=True)
    init_db()
    if TG_TOKEN:
        started = tgpay.start(TG_TOKEN, hooks={
            "lookup_order": _hook_lookup_order,
            "key_for_order": _hook_key_for_order,
            "invoice_url": _hook_invoice_url,
            "link_chat": _hook_link_chat,
            "keys_by_chat": _hook_keys_by_chat,
            "public_base": PUBLIC_BASE,
        })
        if not started:
            print("[server] telegram bot FAILED to start (check TELEGRAM_BOT_TOKEN)",
                  flush=True)
    else:
        print("[server] telegram bot: TELEGRAM_BOT_TOKEN not set — "
              "no chat delivery of keys", flush=True)


@app.get("/status")
def status():
    return {"mode": "mock" if MOCK else "live",
            "database": "sqlite" if _is_sqlite() else "connected",
            "signing_key": "configured" if SIGNING else "missing",
            "telegram_bot": "on" if tgpay.running() else "off",
            "price": PRICE, "days": PERIOD_DAYS, "device_cap": DEVICE_CAP}


# ── Key minting ───────────────────────────────────────────────────

def _bind_device(cur, kid: str, device_id: str, now: int) -> None:
    """Purchase = activation: the buying device is bound at mint time
    (idempotent, and never over the DEVICE_CAP)."""
    if not device_id:
        return
    cur.execute(_sql("SELECT COUNT(*) FROM activations WHERE kid=?"), (kid,))
    if int(cur.fetchone()[0]) >= DEVICE_CAP:
        return
    cur.execute(_sql(
        "INSERT INTO activations(kid, device_id, activated_at) VALUES(?,?,?) "
        "ON CONFLICT (kid, device_id) DO NOTHING"), (kid, device_id, now))


def _mint(order_id: str, prev_key: str, note: str = "") -> dict:
    """Sign a fresh key. Renewal: a valid prev_key extends from its expiry.
    Idempotent per order_id — repeated IPNs return the already-issued key.
    The order's device_id is bound immediately and the order's chat_id is
    stamped on the key, then the key is pushed to that chat."""
    priv = _require_signing()
    now = int(time.time())
    base = now
    if prev_key:
        try:
            payload = keysign.parse_and_verify(keysign.public_key_hex(priv), prev_key)
            base = max(now, int(payload["exp"]))
        except Exception:
            base = now  # unverifiable prev key: treat as a fresh purchase
    kid = keysign.new_kid()
    exp = base + PERIOD_DAYS * 86400
    key_str = keysign.sign_key(priv, now, exp, kid)
    device_id = chat_id = ""
    existing = None
    with get_db() as conn:
        cur = conn.cursor()
        if order_id:
            cur.execute(_sql("SELECT device_id, chat_id FROM orders WHERE order_id=?"),
                        (order_id,))
            orow = cur.fetchone()
            if orow:
                device_id = (orow[0] or "").strip()
                chat_id = (orow[1] or "").strip()
            cur.execute(_sql("SELECT key_str, exp, kid FROM issued_keys WHERE order_id=?"),
                        (order_id,))
            row = cur.fetchone()
            if row:
                _bind_device(cur, row[2], device_id, now)
                cur.close()
                existing = {"key": row[0], "exp": int(row[1]), "kid": row[2]}
        if existing is None:
            cur.execute(_sql(
                "INSERT INTO issued_keys(kid,key_str,order_id,note,iat,exp,revoked,chat_id) "
                "VALUES(?,?,?,?,?,?,0,?)"),
                (kid, key_str, order_id, note, now, exp, chat_id))
            _bind_device(cur, kid, device_id, now)
            cur.close()
    if existing is not None:
        issued = existing
    else:
        print(f"[mint] kid={kid} order={order_id or '-'} exp={exp} "
              f"device={device_id or '-'} chat={chat_id or '-'}", flush=True)
        issued = {"key": key_str, "exp": exp, "kid": kid}
    if order_id:
        _notify_chat(order_id, issued)
    return issued


# ── Telegram delivery (hooks + notify) ────────────────────────────

def _hook_lookup_order(order_id: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT prev_key FROM orders WHERE order_id=?"), (order_id,))
        row = cur.fetchone()
        cur.close()
    return {"prev_key": row[0] or ""} if row else None


def _hook_key_for_order(order_id: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT key_str, exp FROM issued_keys WHERE order_id=?"),
                    (order_id,))
        row = cur.fetchone()
        cur.close()
    return (row[0], int(row[1])) if row else None


def _hook_invoice_url(order_id: str, prev_key: str) -> str:
    try:
        return (f"{PUBLIC_BASE}/mock-pay/{order_id}" if MOCK
                else _nowpayments_invoice(order_id))
    except Exception as exc:
        print(f"[tg] invoice for {order_id} failed: {exc}", flush=True)
        return ""


def _hook_link_chat(order_id: str, chat_id) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("UPDATE orders SET chat_id=? WHERE order_id=?"),
                    (str(chat_id), str(order_id)))
        cur.close()


def _hook_keys_by_chat(chat_id) -> list:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql(
            "SELECT kid, key_str, exp, revoked FROM issued_keys WHERE chat_id=? "
            "ORDER BY exp DESC"), (str(chat_id),))
        rows = cur.fetchall()
        cur.close()
    return [{"kid": r[0], "key_str": r[1], "exp": int(r[2]),
             "revoked": bool(int(r[3]))} for r in rows]


def _notify_chat(order_id: str, issued: dict) -> None:
    """Push the freshly minted key to the chat that paid for the order.
    Quiet no-op when no chat is linked or the bot is not running."""
    if not order_id:
        return
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT chat_id FROM orders WHERE order_id=?"), (order_id,))
        row = cur.fetchone()
        cur.close()
    chat_id = row[0] if row else ""
    if not chat_id:
        return
    when = time.strftime("%Y-%m-%d", time.gmtime(int(issued["exp"])))
    text = (f"Payment received — Interview Assistant\n\n"
            f"Your license key (valid until {when}):\n\n"
            f"{issued['key']}\n\n"
            f"Redeem: myapp://license?key={issued['key']}\n\n"
            f"Payment page: {PUBLIC_BASE}/success/{order_id}\n"
            f"The key works on up to {DEVICE_CAP} devices.")
    tgpay.send_message(chat_id, text,
                       button=("Open key page", f"{PUBLIC_BASE}/success/{order_id}"))



# ── Purchase flow ─────────────────────────────────────────────────

def _nowpayments_invoice(order_id: str) -> str:
    payload = {
        "price_amount": PRICE, "price_currency": "USD",
        "order_id": order_id,
        "order_description": f"Interview Assistant — {PERIOD_DAYS} days",
        "success_url": f"{PUBLIC_BASE}/success/{order_id}",
        "cancel_url": f"{PUBLIC_BASE}/success/{order_id}",
    }
    ipn = os.getenv("IPN_CALLBACK_URL", "").strip()
    if ipn:
        payload["ipn_callback_url"] = ipn
    req = urllib.request.Request(
        "https://api.nowpayments.io/v1/invoice",
        data=json.dumps(payload).encode(),
        headers={"x-api-key": NOW_API, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise HTTPException(502, f"NOWPayments rejected invoice (HTTP {exc.code}): {detail}")
    try:
        return body["invoice_url"]
    except KeyError:
        raise HTTPException(502, f"NOWPayments gave no invoice_url: {json.dumps(body)[:500]}")


@app.post("/invoice")
def invoice(body: dict):
    prev_key = (body.get("prev_key") or "").strip()
    if prev_key:
        try:
            keysign.parse_and_verify(keysign.public_key_hex(_require_signing()), prev_key)
        except ValueError as exc:
            raise HTTPException(400, f"previous key is not valid: {exc}")
    # The buying device travels with the order: purchase = activation.
    device_id = str(body.get("device_id") or "").strip()[:64]
    order_id = "ORD-" + secrets.token_urlsafe(12)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql(
            "INSERT INTO orders(order_id, prev_key, created, device_id) "
            "VALUES(?,?,?,?)"), (order_id, prev_key, int(time.time()), device_id))
        cur.close()
    url = (f"{PUBLIC_BASE}/mock-pay/{order_id}" if MOCK
           else _nowpayments_invoice(order_id))
    resp = {"order_id": order_id, "invoice_url": url,
            "price": PRICE, "days": PERIOD_DAYS, "device_cap": DEVICE_CAP}
    # Deep link so the buyer can finish inside Telegram (bot replies with the
    # invoice and later delivers the key to that chat).
    link = tgpay.deep_link(order_id)
    if link:
        resp["bot_start"] = link
        resp["bot_username"] = tgpay.bot_username()
    return resp


@app.get("/order/{order_id}/status")
def order_status(order_id: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT key_str, exp, kid FROM issued_keys WHERE order_id=?"),
                    (order_id,))
        row = cur.fetchone()
        if row:
            cur.close()
            return {"status": "issued", "key": row[0], "exp": int(row[1]), "kid": row[2]}
        cur.execute(_sql("SELECT created FROM orders WHERE order_id=?"), (order_id,))
        known = cur.fetchone() is not None
        cur.close()
    return {"status": "pending" if known else "unknown"}


def _handle_payment(order_id: str, payment_id: str, status: str, raw: str = "") -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql(
            "INSERT INTO payments(payment_id,order_id,status,raw,ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT (payment_id) DO NOTHING"),
            (payment_id or f"{order_id}-{int(time.time())}", order_id, status,
             raw[:4000], int(time.time())))
        cur.close()
    if status in ("finished", "confirmed") and order_id:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(_sql("SELECT prev_key FROM orders WHERE order_id=?"), (order_id,))
            row = cur.fetchone()
            cur.close()
        _mint(order_id, row[0] if row else "")


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
    _handle_payment(str(data.get("order_id", "")),
                    str(data.get("payment_id", "")),
                    str(data.get("payment_status", "")),
                    raw.decode(errors="replace"))
    return JSONResponse({"ok": True})


@app.get("/mock-pay/{order_id}")
def mock_pay(order_id: str):
    if not MOCK:
        raise HTTPException(404, "not found")
    _handle_payment(order_id, f"MOCK-{order_id}", "finished", "mock")
    return RedirectResponse(f"/success/{order_id}", status_code=302)


# ── Success page ──────────────────────────────────────────────────

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<title>Interview Assistant — your license key</title>
<style>
 body {{ background:#0b0f14; color:#c9d5e0; font:15px/1.5 system-ui,sans-serif;
        max-width:640px; margin:48px auto; padding:0 20px; }}
 h1 {{ color:#4fd1ff; font-weight:600; }}
 .key {{ background:#121a24; border:1px solid #2a3a4d; border-radius:8px;
        padding:14px 16px; font:14px/1.4 Consolas,monospace; color:#9ef01a;
        word-break:break-all; user-select:all; }}
 .muted {{ color:#7a8a9a; font-size:13px; }}
 a {{ color:#4fd1ff; }}
</style></head><body>
<h1>Interview Assistant</h1>
{body}
<p class="muted">Order <code>{order}</code> · ${price} / {days} days</p>
</body></html>"""


def _success_page(order_id: str) -> HTMLResponse:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT key_str, exp FROM issued_keys WHERE order_id=?"), (order_id,))
        row = cur.fetchone()
        cur.close()
    if row:
        key, exp = row[0], int(row[1])
        body = (f"<p>Payment received. Your license key (valid until "
                f"{time.strftime('%Y-%m-%d', time.gmtime(exp))}):</p>"
                f'<div class="key">{escape(key)}</div>'
                "<p>Copy it and paste it into the app's license window. "
                "The key works on up to {cap} devices.</p>").format(cap=DEVICE_CAP)
        refresh = ""
    else:
        body = ("<p>Payment not confirmed yet — this page refreshes every 5 seconds. "
                "Paid but stuck? Give it a couple of minutes; if it never appears, "
                "contact support with your order id.</p>"
                '<p class="muted">You can close this tab and come back later — '
                "the app is also watching this order and will fill the key in for you.</p>")
        refresh = '5'
    return HTMLResponse(_PAGE.format(refresh=refresh, body=body,
                                     order=escape(order_id), price=PRICE, days=PERIOD_DAYS))


@app.get("/success/{order_id}")
def success(order_id: str):
    return _success_page(order_id)


# ── Activation + soft recheck ─────────────────────────────────────

@app.post("/activate")
def activate(body: dict):
    priv = _require_signing()
    key = (body.get("key") or "").strip()
    device_id = (body.get("device_id") or "").strip()[:64]
    if not key or not device_id:
        raise HTTPException(400, "key + device_id required")
    try:
        payload = keysign.parse_and_verify(keysign.public_key_hex(priv), key)
    except ValueError as exc:
        raise HTTPException(403, f"license invalid: {exc}")
    kid, exp = str(payload["kid"]), int(payload["exp"])
    if exp < time.time():
        raise HTTPException(403, "license expired")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT revoked FROM issued_keys WHERE kid=?"), (kid,))
        row = cur.fetchone()
        if row and int(row[0]):
            cur.close()
            raise HTTPException(403, "license revoked")
        cur.execute(_sql("SELECT device_id FROM activations WHERE kid=? AND device_id=?"),
                    (kid, device_id))
        already = cur.fetchone() is not None
        if not already:
            cur.execute(_sql("SELECT COUNT(*) FROM activations WHERE kid=?"), (kid,))
            count = int(cur.fetchone()[0])
            if count >= DEVICE_CAP:
                cur.close()
                raise HTTPException(
                    403, f"device cap reached — this key is already active on "
                         f"{count} devices (limit {DEVICE_CAP})")
            cur.execute(_sql(
                "INSERT INTO activations(kid, device_id, activated_at) VALUES(?,?,?)"),
                (kid, device_id, int(time.time())))
        cur.execute(_sql("SELECT COUNT(*) FROM activations WHERE kid=?"), (kid,))
        count = int(cur.fetchone()[0])
        cur.close()
    return {"ok": True, "exp": exp, "kid": kid, "devices": count,
            "remaining": max(0, DEVICE_CAP - count)}


@app.post("/recheck")
def recheck(body: dict):
    """Soft revocation check. ok=False means revoked; the key's own signature
    and expiry remain authoritative for everything else."""
    kid = (body.get("kid") or "").strip()
    device_id = (body.get("device_id") or "").strip()
    if not kid:
        raise HTTPException(400, "kid required")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT revoked, exp FROM issued_keys WHERE kid=?"), (kid,))
        row = cur.fetchone()
        activated = False
        if device_id:
            cur.execute(_sql("SELECT 1 FROM activations WHERE kid=? AND device_id=?"),
                        (kid, device_id))
            activated = cur.fetchone() is not None
        cur.close()
    revoked = bool(row and int(row[0]))
    return {"ok": not revoked, "revoked": revoked,
            "exp": int(row[1]) if row else 0, "activated": activated}


# ── Admin ─────────────────────────────────────────────────────────

def _admin_check(token: str):
    if not ADMIN_TOKEN or not secrets.compare_digest(str(token or ""), ADMIN_TOKEN):
        raise HTTPException(401, "bad admin token")


@app.post("/admin/mint")
def admin_mint(body: dict):
    """Mint a standalone key by hand — comps, or recovering a buyer whose
    payment landed while the server was asleep."""
    _admin_check(body.get("admin_token", ""))
    days = int(body.get("days", PERIOD_DAYS))
    now = int(time.time())
    priv = _require_signing()
    kid = keysign.new_kid()
    exp = now + days * 86400
    key_str = keysign.sign_key(priv, now, exp, kid)
    note = str(body.get("note", ""))[:200]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql(
            "INSERT INTO issued_keys(kid,key_str,order_id,note,iat,exp,revoked) "
            "VALUES(?,?,?,?,?,?,0)"),
            (kid, key_str, "", note, now, exp))
        cur.close()
    return {"ok": True, "key": key_str, "kid": kid, "exp": exp}


@app.post("/admin/revoke")
def admin_revoke(body: dict):
    _admin_check(body.get("admin_token", ""))
    kid = (body.get("kid") or "").strip()
    if not kid:
        raise HTTPException(400, "kid required")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("UPDATE issued_keys SET revoked=1 WHERE kid=?"), (kid,))
        cur.close()
    return {"ok": True}


@app.post("/admin/lookup")
def admin_lookup(body: dict):
    _admin_check(body.get("admin_token", ""))
    order_id = (body.get("order_id") or "").strip()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT key_str, exp, kid, revoked FROM issued_keys WHERE order_id=?"),
                    (order_id,))
        row = cur.fetchone()
        cur.execute(_sql("SELECT COUNT(*) FROM issued_keys"))
        total = int(cur.fetchone()[0])
        cur.close()
    if not row:
        return {"exists": False, "total_keys": total}
    return {"exists": True, "total_keys": total, "key": row[0], "exp": int(row[1]),
            "kid": row[2], "revoked": bool(int(row[3]))}
