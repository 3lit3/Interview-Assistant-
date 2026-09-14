"""License + key-issuance server: $9.99/mo, 1 seat per key, NOWPayments-backed.

Endpoints:
  POST /invoice   {hwid, plan} -> {key, invoice_url}
  POST /ipn       (NOWPayments webhook, HMAC-verified) -> extends 30 days
  POST /activate  {key, hwid} -> binds device on first use, checks sub active
  POST /verify    {key, hwid} -> same as activate (called at startup + 30min)
  POST /issue-key {key, hwid} -> {blob, verification, algo} (encrypted LLM key)

New: /issue-key encrypts the real LLM API key per-device using AES-256-CBC.
The client decrypts it locally and uses it for all LLM calls. The key is never
sent in plaintext. Env var LLM_API_KEY holds the real key on the server.
Env var KEY_ISSUANCE_SECRET is the AES encryption master secret.

Run:  uvicorn app:app --port 8000   (from server/ dir)
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

from crypto_utils import issue_encrypted_key

DB = os.path.join(os.path.dirname(__file__), "licenses.db")
PRICE = float(os.getenv("PRICE_USD", "9.99"))
CURRENCY = os.getenv("PRICE_CURRENCY", "USD")
PERIOD_DAYS = 30
NOW_API = os.getenv("NOWPAYMENTS_API_KEY", "")
NOW_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
MOCK = os.getenv("MOCK_NOWPAYMENTS", "1") == "1"
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
KEY_ISSUANCE_SECRET = os.getenv("KEY_ISSUANCE_SECRET", "")

app = FastAPI(title="InterviewAssistant License + Key Server")


@app.on_event("startup")
async def _log_mode():
    print(f"[license] mode={'MOCK' if MOCK else 'LIVE'} price={PRICE}{CURRENCY} "
          f"api_key_set={bool(NOW_API)} ipn_secret_set={bool(NOW_IPN_SECRET)} "
          f"llm_key_set={bool(LLM_API_KEY)} key_secret_set={bool(KEY_ISSUANCE_SECRET)}",
          flush=True)


@app.get("/status")
async def status():
    return {"mode": "mock" if MOCK else "live",
            "api_key_set": bool(NOW_API), "ipn_secret_set": bool(NOW_IPN_SECRET),
            "llm_key_set": bool(LLM_API_KEY), "key_secret_set": bool(KEY_ISSUANCE_SECRET),
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


@app.post("/invoice")
async def invoice(body: dict):
    hwid = (body.get("hwid") or "")[:64]
    key = (body.get("key") or "").strip() or new_key()
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
    if not row:
        c.execute("INSERT INTO licenses(key,hwid,status,current_period_end,created) VALUES(?,'', 'active',0,?)",
                  (key, int(time.time())))
        base = 0
    else:
        base = int(row[0])
    base = max(int(time.time()), base)
    new_end = base + PERIOD_DAYS * 86400
    c.execute("UPDATE licenses SET status='active', current_period_end=? WHERE key=?", (new_end, key))
    c.commit()
    c.close()
    return new_end


@app.post("/admin/extend")
async def admin_extend(body: dict):
    token = os.getenv("ADMIN_TOKEN", "")
    if not token or not secrets.compare_digest(str(body.get("admin_token", "")), token):
        raise HTTPException(401, "bad admin token")
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "key required")
    days = int(body.get("days", PERIOD_DAYS))
    c = db()
    row = c.execute("SELECT current_period_end FROM licenses WHERE key=?", (key,)).fetchone()
    created = row is None
    if not row:
        c.execute("INSERT INTO licenses(key,hwid,status,current_period_end,created) VALUES(?,'', 'active',0,?)",
                  (key, int(time.time())))
        base = 0
    else:
        base = int(row[0])
    new_end = max(int(time.time()), base) + days * 86400
    c.execute("UPDATE licenses SET status='active', current_period_end=? WHERE key=?", (new_end, key))
    c.commit()
    c.close()
    return {"ok": True, "exp": new_end, "created": created}


@app.post("/admin/lookup")
async def admin_lookup(body: dict):
    token = os.getenv("ADMIN_TOKEN", "")
    if not token or not secrets.compare_digest(str(body.get("admin_token", "")), token):
        raise HTTPException(401, "bad admin token")
    key = (body.get("key") or "").strip()
    c = db()
    row = c.execute("SELECT status, current_period_end, hwid FROM licenses WHERE key=?", (key,)).fetchone()
    n = c.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
    c.close()
    if not row:
        return {"exists": False, "total_keys": n}
    status, end, hwid = row
    return {"exists": True, "status": status, "exp": int(end),
            "active_now": status == "active" and int(end) > int(time.time()),
            "bound": bool(hwid), "total_keys": n}


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
        c.execute("UPDATE licenses SET hwid=? WHERE key=?", (hwid, key))
        c.commit()
    c.close()
    ok, msg, end = _check(key, hwid)
    return {"ok": ok, "error": None if ok else msg, "exp": end}


@app.post("/verify")
async def verify(body: dict):
    return await activate(body)


@app.post("/issue-key")
async def issue_key(body: dict):
    """Issue an encrypted LLM API key for a verified device.

    Client calls this at every startup after license verification.
    Returns AES-256-CBC encrypted blob that only decrypts on the
    requesting device (HWID-bound).
    """
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
    c = db()
    c.execute("INSERT OR REPLACE INTO payments(payment_id,key,status,raw,ts) VALUES(?,?,?,?,?)",
              (pid or order_id + str(time.time()), order_id, status, raw.decode()[:4000], int(time.time())))
    c.commit()
    c.close()
    if status in ("finished", "confirmed") and order_id:
        _extend(order_id)
    return JSONResponse({"ok": True})


@app.post("/simulate_pay")
async def simulate_pay(body: dict):
    if not MOCK:
        raise HTTPException(403, "disabled when MOCK_NOWPAYMENTS=0")
    return {"ok": True, "exp": _extend((body.get("key") or "").strip())}
