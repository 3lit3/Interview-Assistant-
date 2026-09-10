"""License server stub: $9.99/mo, 1 seat per key, NOWPayments-backed.

Endpoints:
  POST /invoice  {hwid, plan} -> {key, invoice_url}   (creates pending key)
  POST /ipn      (NOWPayments webhook, HMAC-verified) -> extends 30 days
  POST /activate {key, hwid} -> binds device on first use, checks sub active
  POST /verify   {key, hwid} -> same as activate (called at startup + 30min)

Setup (NOWPayments dashboard):
  1. Get API key + IPN secret.
  2. Set IPN callback URL to https://YOURHOST/ipn
  3. env: NOWPAYMENTS_API_KEY, NOWPAYMENTS_IPN_SECRET (see .env.example)

Run:  uvicorn app:app --port 8000   (from server/ dir)
Test without NOWPayments keys: MOCK_NOWPAYMENTS=1 -> /invoice returns a
fake pay URL and /ipn can be simulated via POST /simulate_pay {key}.

Billing model: each confirmed $9.99 payment extends current_period_end by
30 days. No auto-rebill (crypto has no chargeable token) — user pays a new
invoice each month via /invoice using their existing key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.request

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

DB = os.path.join(os.path.dirname(__file__), "licenses.db")
PRICE = float(os.getenv("PRICE_USD", "9.99"))
CURRENCY = os.getenv("PRICE_CURRENCY", "USD")
PERIOD_DAYS = 30
NOW_API = os.getenv("NOWPAYMENTS_API_KEY", "")
NOW_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
MOCK = os.getenv("MOCK_NOWPAYMENTS", "1") == "1"  # default mock until keys set

app = FastAPI(title="InterviewAssistant License Server")


@app.on_event("startup")
async def _log_mode():
    # Safe: logs mode only, never secrets. Visible in Render -> Logs.
    print(f"[license] mode={'MOCK' if MOCK else 'LIVE'} price={PRICE}{CURRENCY} "
          f"api_key_set={bool(NOW_API)} ipn_secret_set={bool(NOW_IPN_SECRET)}", flush=True)


@app.get("/status")
async def status():
    """Safe diagnostic: mode flags only, no secrets, no keys."""
    return {"mode": "mock" if MOCK else "live",
            "api_key_set": bool(NOW_API), "ipn_secret_set": bool(NOW_IPN_SECRET),
            "price": PRICE, "currency": CURRENCY, "days": PERIOD_DAYS}


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS licenses(
      key TEXT PRIMARY KEY, hwid TEXT, status TEXT DEFAULT 'pending',
      current_period_end INTEGER DEFAULT 0, created INTEGER, email TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments(
      payment_id TEXT PRIMARY KEY, key TEXT, status TEXT, raw TEXT, ts INTEGER)""")
    return c


def new_key() -> str:
    return "IA-" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24].upper()


def nowpayments_invoice(order_id: str) -> str:
    """Real NOWPayments invoice; falls back to mock URL when no API key."""
    if not NOW_API or MOCK:
        return f"http://127.0.0.1:8000/pay/MOCK-{order_id} (set NOWPAYMENTS_API_KEY for real link)"
    payload = {
        "price_amount": PRICE, "price_currency": CURRENCY,
        "order_id": order_id, "order_description": "Interview Assistant $9.99/mo, 1 seat",
        "ipn_callback_url": os.getenv("IPN_CALLBACK_URL", ""),
        "success_url": os.getenv("SUCCESS_URL", ""), "cancel_url": os.getenv("CANCEL_URL", ""),
    }
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


@app.post("/invoice")
async def invoice(body: dict):
    hwid = (body.get("hwid") or "")[:64]
    key = (body.get("key") or "").strip() or new_key()  # renewal: pass existing key
    c = db()
    row = c.execute("SELECT key FROM licenses WHERE key=?", (key,)).fetchone()
    if not row:
        c.execute("INSERT INTO licenses(key,hwid,status,current_period_end,created) VALUES(?,?, 'pending',0,?)",
                  (key, hwid, int(time.time())))
        c.commit()
    c.close()
    return {"key": key, "invoice_url": nowpayments_invoice(key),
            "price": PRICE, "currency": CURRENCY, "days": PERIOD_DAYS}


def _extend(key: str) -> int:
    c = db()
    row = c.execute("SELECT current_period_end FROM licenses WHERE key=?", (key,)).fetchone()
    base = max(int(time.time()), int(row[0]) if row else 0)
    new_end = base + PERIOD_DAYS * 86400
    c.execute("UPDATE licenses SET status='active', current_period_end=? WHERE key=?", (new_end, key))
    c.commit()
    c.close()
    return new_end


def _check(key: str, hwid: str) -> tuple[bool, str, int]:
    c = db()
    row = c.execute("SELECT hwid,status,current_period_end FROM licenses WHERE key=?", (key,)).fetchone()
    c.close()
    if not row:
        return False, "unknown license key", 0
    bound, status, end = row
    if bound and bound != hwid:
        return False, "key already bound to another device (1 seat)", 0
    if status != "active" or int(end) < time.time():
        return False, "subscription inactive or expired — pay $9.99 to renew", 0
    return True, "ok", int(end)


@app.post("/activate")
async def activate(body: dict):
    key, hwid = (body.get("key") or "").strip(), (body.get("hwid") or "")[:64]
    if not key or not hwid:
        raise HTTPException(400, "key + hwid required")
    c = db()
    row = c.execute("SELECT hwid FROM licenses WHERE key=?", (key,)).fetchone()
    if not row:
        c.close()
        raise HTTPException(404, "unknown key")
    if not row[0]:
        c.execute("UPDATE licenses SET hwid=? WHERE key=?", (hwid, key))  # first bind
        c.commit()
    c.close()
    ok, msg, end = _check(key, hwid)
    return {"ok": ok, "error": None if ok else msg, "exp": end}


@app.post("/verify")
async def verify(body: dict):
    return await activate(body)  # same policy: bound device + active period


@app.post("/ipn")
async def ipn(req: Request, x_nowpayments_sig: str = Header(default="", alias="x-nowpayments-sig")):
    raw = await req.body()
    if NOW_IPN_SECRET:  # NOWPayments signs sorted-JSON with IPN secret
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
    c = db()
    c.execute("INSERT OR REPLACE INTO payments(payment_id,key,status,raw,ts) VALUES(?,?,?,?,?)",
              (pid or order_id + str(time.time()), order_id, status, raw.decode()[:4000], int(time.time())))
    c.commit()
    c.close()
    if status in ("finished", "confirmed") and order_id:
        _extend(order_id)
    return JSONResponse({"ok": True})


@app.post("/simulate_pay")  # mock helper until NOWPayments keys are set
async def simulate_pay(body: dict):
    if not MOCK:
        raise HTTPException(403, "disabled when MOCK_NOWPAYMENTS=0")
    return {"ok": True, "exp": _extend((body.get("key") or "").strip())}
