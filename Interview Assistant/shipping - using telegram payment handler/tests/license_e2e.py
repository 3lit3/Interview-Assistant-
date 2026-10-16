"""Local E2E test: invoice -> mock-pay -> key -> activate(cap) -> recheck -> renewal.

Also covers the Telegram layer: the key is bound to the buying device AT MINT
(no /activate call), the order chat is stamped on the issued key, and the bot
handlers (/start buy_, /mykeys, /help) run offline against the live server with
the Bot API stubbed (no token, no network).
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
PORT = 8077
BASE = f"http://127.0.0.1:{PORT}"
# The client helpers (lic.whoami etc.) must hit THIS server, not prod.
os.environ["LICENSE_SERVER_URL"] = BASE
db = Path(tempfile.gettempdir()) / "ia_license_e2e.db"
if db.exists():
    db.unlink()

env = dict(os.environ)
env.update({
    "MOCK_NOWPAYMENTS": "1",
    "PRICE_USD": "14.99",
    "PERIOD_DAYS": "30",
    "DEVICE_CAP": "2",
    "PUBLIC_BASE_URL": BASE,
    "ADMIN_TOKEN": "e2e-token",
    "DATABASE_URL": f"sqlite:///{db}",
    # Force the delivery bot OFF in the server process (the test drives the
    # handlers itself with the Bot API stubbed).
    "TELEGRAM_BOT_TOKEN": "",
})
# Pull SIGNING_PRIVATE_KEY from server/.env
for line in (ROOT / "server" / ".env").read_text().splitlines():
    if line.startswith("SIGNING_PRIVATE_KEY=") and not env.get("SIGNING_PRIVATE_KEY"):
        env["SIGNING_PRIVATE_KEY"] = line.split("=", 1)[1].strip()
env.setdefault("SIGNING_PRIVATE_KEY", os.environ.get("SIGNING_PRIVATE_KEY", ""))

sys.path.insert(0, str(ROOT / "client"))
import licensing as lic  # noqa: E402


def kid_of(key: str) -> str:
    return json.loads(lic._b64d(key.split(".", 1)[0][4:]))["kid"]

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)

def req(path, data=None, method=None, timeout=30):
    r = urllib.request.Request(BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def db_one(sql, params=()):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def db_exec(sql, params=()):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()

srv = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
                       cwd=str(ROOT / "server"), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(60):
        try:
            st, body = req("/status")
            if st == 200:
                break
        except Exception:
            time.sleep(0.5)
    else:
        print("server never came up"); sys.exit(2)

    status = json.loads(body)
    print("server /status:", status)
    check("mock mode", status.get("mode") == "mock", str(status))
    check("bot off when token empty", status.get("telegram_bot") == "off", str(status))

    # 1. invoice — the buying device travels with the order
    st, body = req("/invoice", {"device_id": "dev-mint"})
    inv = json.loads(body)
    order_id = inv.get("order_id") or inv.get("order")
    check("invoice created", st == 200 and order_id, str(inv))
    check("no bot deep link when bot is off", not inv.get("bot_start"), str(inv))
    print("   invoice_url:", inv.get("invoice_url"))

    # 1b. link a fake chat to the order — this is what /start buy_<order>
    #     does in production; here it is stamped directly (no bot needed).
    db_exec("UPDATE orders SET chat_id=? WHERE order_id=?", ("999001", order_id))

    # 2. mock pay
    st, body = req(f"/mock-pay/{order_id}")
    check("mock-pay", st == 200, body[:200].decode(errors="replace"))

    # 3. order status -> key
    for _ in range(10):
        st, body = req(f"/order/{order_id}/status")
        o = json.loads(body)
        if o.get("status") == "issued" and o.get("key"):
            break
        time.sleep(0.3)
    key = o.get("key")
    check("key minted", o.get("status") == "issued" and key and key.startswith("3L3-"), str(o))
    kid = kid_of(key) if key else ""

    # 4. success page shows the key
    st, body = req(f"/success/{order_id}")
    check("success page has key", st == 200 and b"3L3-" in body)

    # 5. purchase = activation: dev-mint was bound AT MINT, no /activate call
    st, body = req("/recheck", {"kid": kid, "device_id": "dev-mint"})
    r = json.loads(body)
    check("device bound at mint (no /activate)", r.get("activated") is True, str(r))
    check("minted key recheck ok", r.get("ok") is True, str(r))
    row = db_one("SELECT chat_id FROM issued_keys WHERE kid=?", (kid,))
    check("issued key stamped with chat_id", row and row[0] == "999001", str(row))

    # 5b. cap: the minted device already counts — one more fills it, third fails
    st, body = req("/activate", {"key": key, "device_id": "dev-A"})
    a = json.loads(body)
    check("activate dev-A (2nd device)", st == 200 and a.get("ok")
          and int(a.get("devices", 0)) == 2, str(a))
    st, body = req("/activate", {"key": key, "device_id": "dev-B"})
    a = json.loads(body)
    check("3rd device rejected", (st != 200) or (not a.get("ok")), f"st={st} {a}")
    check("cap error mentions 2", "2" in str(a), str(a))

    # 5c. re-activate same device is idempotent (reinstall)
    st, body = req("/activate", {"key": key, "device_id": "dev-A"})
    a = json.loads(body)
    check("re-activate same device ok", st == 200 and a.get("ok"), str(a))

    # 6. recheck ok
    st, body = req("/recheck", {"kid": kid, "device_id": "dev-A"})
    r = json.loads(body)
    check("recheck ok", r.get("ok") is True, str(r))

    # 7. revoke -> recheck false
    st, body = req("/admin/revoke", {"admin_token": "e2e-token", "kid": kid})
    check("admin revoke", st == 200 and json.loads(body).get("ok"), body[:200].decode(errors="replace"))
    st, body = req("/recheck", {"kid": kid, "device_id": "dev-A"})
    r = json.loads(body)
    check("recheck after revoke", r.get("ok") is False, str(r))

    # 8. renewal: new invoice with prev_key extends from old expiry
    st, body = req("/invoice", {"prev_key": key})
    inv2 = json.loads(body)
    st, body = req(f"/mock-pay/{inv2['order_id']}")
    for _ in range(10):
        st, body = req(f"/order/{inv2['order_id']}/status")
        o2 = json.loads(body)
        if o2.get("key"):
            break
        time.sleep(0.3)
    key2 = o2.get("key")
    check("renewal key minted", bool(key2) and key2 != key)
    # client-side offline validation of the minted key (pubkey match)
    payload, why = lic.validate_local(key2)
    check("client offline validate (pubkey match)", payload is not None, why)
    payload1, _ = lic.validate_local(key)
    if payload1 and payload:
        check("renewal extends expiry", payload["exp"] >= payload1["exp"],
              f"{payload['exp']} vs {payload1['exp']}")
    # tampered key fails
    tampered = key2[:-4] + ("AAAA" if not key2.endswith("AAAA") else "BBBB")
    p, why = lic.validate_local(tampered)
    check("tampered key rejected", p is None, why)
    # junk fails
    p, why = lic.validate_local("hello")
    check("junk rejected", p is None, why)

    # 9. Telegram layer: bot handlers run in-process against the live server
    #    with the Bot API stubbed (no token, no network, no long-polling).
    st, body = req("/invoice", {"device_id": "dev-tg"})
    inv3 = json.loads(body)
    oid3 = inv3.get("order_id")
    check("telegram order created", st == 200 and bool(oid3), str(inv3))

    # Importing server/app.py into THIS process: it must see the same env as
    # the server subprocess (its own .env loader would otherwise pick defaults).
    for _k in ("MOCK_NOWPAYMENTS", "PRICE_USD", "PERIOD_DAYS", "DEVICE_CAP",
               "PUBLIC_BASE_URL", "ADMIN_TOKEN", "DATABASE_URL",
               "TELEGRAM_BOT_TOKEN", "SIGNING_PRIVATE_KEY"):
        os.environ[_k] = env[_k]
    sys.path.insert(0, str(ROOT / "server"))
    import app as app_mod  # noqa: E402
    import tgpay  # noqa: E402
    # Load SIGNING + schema in THIS process too (the endpoint functions are
    # also called directly below with the Bot API stubbed). The bot stays off
    # in-process: TELEGRAM_BOT_TOKEN is empty, so start() is skipped.
    app_mod._startup()

    sent = []

    def _fake_api(method, payload=None, timeout=45):
        sent.append((method, payload or {}))
        return {"ok": True, "result": {"message_id": len(sent)}}

    tgpay._api = _fake_api
    tgpay._token = "test-token"
    tgpay._username = "FakeIABot"
    tgpay._hooks = {
        "lookup_order": app_mod._hook_lookup_order,
        "key_for_order": app_mod._hook_key_for_order,
        "invoice_url": app_mod._hook_invoice_url,
        "link_chat": app_mod._hook_link_chat,
        "keys_by_chat": app_mod._hook_keys_by_chat,
        "public_base": BASE,
    }

    def drive(text, chat=515151):
        sent.clear()
        tgpay._handle({"message": {"chat": {"id": chat}, "text": text}})
        joined = "".join(str((p or {}).get("text", "")) for _, p in sent)
        last = sent[-1][1] if sent else {}
        return joined, last

    # /start buy_<order> on an unpaid order -> invoice button + chat linked
    txt, last = drive(f"/start buy_{oid3}")
    check("/start buy_ replies", bool(txt), str(sent[:1]))
    kb = last.get("reply_markup", {}).get("inline_keyboard", [])
    check("invoice URL button",
          bool(kb) and str(kb[0][0].get("url", "")).endswith(f"/mock-pay/{oid3}"),
          str(last))
    row = db_one("SELECT chat_id FROM orders WHERE order_id=?", (oid3,))
    check("/start links orders.chat_id", row and row[0] == "515151", str(row))

    # pay -> mint (device bound + chat stamped) -> /start hands over the key
    req(f"/mock-pay/{oid3}")
    key3, o3 = "", {}
    for _ in range(10):
        st, body = req(f"/order/{oid3}/status")
        o3 = json.loads(body)
        if o3.get("key"):
            key3 = o3["key"]
            break
        time.sleep(0.3)
    kid3 = kid_of(key3) if key3 else ""
    check("telegram key minted", bool(key3), str(o3))

    txt, _ = drive(f"/start buy_{oid3}")
    check("key delivered by /start", bool(key3) and key3 in txt, txt[:200])
    check("myapp link delivered", f"myapp://license?key={key3}" in txt, txt[:200])
    row = db_one("SELECT chat_id FROM issued_keys WHERE kid=?", (kid3,))
    check("delivered key stamped with chat_id", row and row[0] == "515151", str(row))
    st, body = req("/recheck", {"kid": kid3, "device_id": "dev-tg"})
    r3 = json.loads(body)
    check("telegram order bound at mint", r3.get("activated") is True, str(r3))

    # /mykeys, /help, unknown order
    txt, _ = drive("/mykeys")
    check("/mykeys lists the key", bool(key3) and key3 in txt, txt[:200])
    txt, _ = drive("/help")
    check("/help answers", "license bot" in txt.lower(), txt[:120])
    txt, _ = drive("/start buy_ORD-does-not-exist")
    check("unknown order handled", "don't know order" in txt, txt[:120])

    # helpers with the bot switched off
    tgpay._token = ""
    tgpay._username = ""
    tgpay._hooks = {}
    check("send_message no-op without token", tgpay.send_message(1, "hi") is False)
    check("deep_link empty without bot", tgpay.deep_link(oid3) == "")
    tgpay._username = "FakeIABot"
    check("deep_link format",
          tgpay.deep_link("ORD-abc") == "https://t.me/FakeIABot?start=buy_ORD-abc")
    tgpay._username = ""

    # ── 10. Zero-config Telegram: whoami, relay send, payment-chat link ──
    # Same stubbed Bot API as section 9 for in-process handler calls; the
    # HTTP endpoints run against the live subprocess (shared sqlite file).
    tgpay._api = _fake_api
    tgpay._token = "test-token"
    tgpay._username = "FakeIABot"
    tgpay._hooks = {
        "lookup_order": app_mod._hook_lookup_order,
        "key_for_order": app_mod._hook_key_for_order,
        "invoice_url": app_mod._hook_invoice_url,
        "link_chat": app_mod._hook_link_chat,
        "keys_by_chat": app_mod._hook_keys_by_chat,
        "link_nonce": app_mod._hook_link_nonce,
        "public_base": BASE,
    }

    # whoami: the chat stamped on key3 at mint (HTTP, client helper)
    w = lic.whoami(key3)
    check("whoami returns linked chat", w.get("chat_id") == "515151", str(w))
    check("whoami returns kid", w.get("kid") == kid3, str(w))
    w = app_mod.whoami({"key": key3})
    check("whoami bot_username (stubbed bot)", w.get("bot_username") == "FakeIABot",
          str(w))

    # auth: junk keys rejected everywhere
    st, body = req("/whoami", {"key": "3L3-junk"})
    check("whoami rejects junk key", st == 403, f"st={st}")
    st, body = req("/tg/send", {"key": "3L3-junk", "op": "push", "text": "x"})
    check("tg/send rejects junk key", st == 403, f"st={st}")
    st, body = req("/link/status", {"key": "3L3-junk", "nonce": "n"})
    check("link/status rejects junk key", st == 403, f"st={st}")
    st, body = req("/whoami", {"key": key})          # revoked in step 7
    check("whoami rejects revoked key", st == 403, f"st={st} {body[:80]}")

    # renewal key2: valid but never linked to any chat
    st, body = req("/whoami", {"key": key2})
    d = json.loads(body)
    check("whoami empty chat when unlinked",
          st == 200 and d.get("chat_id") == "", str(d))
    st, body = req("/tg/send", {"key": key2, "op": "push", "text": "x"})
    check("tg/send 409 without linked chat", st == 409, f"st={st}")

    # relay ops through the in-process handlers (bot stubbed)
    sent.clear()
    r = app_mod.tg_send({"key": key3, "op": "push", "text": "relay hello"})
    check("relay push ok + message_id",
          r.get("ok") is True and (r.get("result") or {}).get("message_id") == 1
          and sent and sent[-1][1].get("text") == "relay hello", str(r))
    r = app_mod.tg_send({"key": key3, "op": "edit", "text": "edited",
                         "message_id": 7})
    check("relay edit ok",
          r.get("ok") is True and sent[-1][0] == "editMessageText", str(r))
    r = app_mod.tg_send({"key": key3, "op": "delete", "message_id": 7})
    check("relay delete ok",
          r.get("ok") is True and sent[-1][0] == "deleteMessage", str(r))
    r = app_mod.tg_send({"key": key3, "op": "edit", "text": "x"})
    check("relay edit without message_id refused", r.get("ok") is False, str(r))
    try:
        app_mod.tg_send({"key": "3L3-junk", "op": "push", "text": "x"})
        check("relay auth raises 403", False, "no exception")
    except Exception as exc:
        check("relay auth raises 403", getattr(exc, "status_code", 0) == 403,
              repr(exc))
    try:
        app_mod.tg_send({"key": key2, "op": "push", "text": "x"})
        check("relay no-chat raises 409", False, "no exception")
    except Exception as exc:
        check("relay no-chat raises 409", getattr(exc, "status_code", 0) == 409,
              repr(exc))

    # sliding-window rate limit per key (30 ops / 5 s → 429)
    got_429 = False
    for i in range(40):
        try:
            app_mod.tg_send({"key": key3, "op": "push", "text": f"spam {i}"})
        except Exception as exc:
            if getattr(exc, "status_code", 0) == 429:
                got_429 = True
                break
    check("relay rate limit kicks in", got_429)

    # payment-chat link: bot /start link_<nonce> → /link/status consumes once.
    # Clear the stamps first so the link flow has to restore both tables.
    db_exec("UPDATE issued_keys SET chat_id='' WHERE kid=?", (kid3,))
    db_exec("UPDATE orders SET chat_id='' WHERE order_id=?", (oid3,))
    txt, _ = drive("/start link_E2ENONCE1")
    check("/start link_ confirms", "linked" in txt.lower(), txt[:160])
    row = db_one("SELECT chat_id FROM link_challenges WHERE nonce=?",
                 ("E2ENONCE1",))
    check("link challenge recorded", row and row[0] == "515151", str(row))
    st, body = req("/link/status", {"key": key3, "nonce": "E2ENONCE1"})
    d = json.loads(body)
    check("link/status returns the chat",
          st == 200 and d.get("chat_id") == "515151", str(d))
    row = db_one("SELECT chat_id FROM issued_keys WHERE kid=?", (kid3,))
    check("link stamps issued key", row and row[0] == "515151", str(row))
    row = db_one("SELECT chat_id FROM orders WHERE order_id=?", (oid3,))
    check("link stamps order", row and row[0] == "515151", str(row))
    st, body = req("/link/status", {"key": key3, "nonce": "E2ENONCE1"})
    check("link nonce is single-use",
          json.loads(body).get("chat_id") == "", str(body))
    st, body = req("/link/status", {"key": key3, "nonce": "E2ENEVER"})
    check("unknown nonce -> no chat",
          st == 200 and json.loads(body).get("chat_id") == "", str(body))

    # expired nonce (bot tapped Start only after 10+ minutes)
    drive("/start link_E2EEXPIRE")
    db_exec("UPDATE link_challenges SET ts=? WHERE nonce=?",
            (int(time.time()) - 700, "E2EEXPIRE"))
    st, body = req("/link/status", {"key": key3, "nonce": "E2EEXPIRE"})
    check("expired nonce -> no chat",
          json.loads(body).get("chat_id") == "", str(body))

    # a /start after mint re-stamps a key that was delivered chat-less
    db_exec("UPDATE issued_keys SET chat_id='' WHERE kid=?", (kid3,))
    drive(f"/start buy_{oid3}")
    row = db_one("SELECT chat_id FROM issued_keys WHERE kid=?", (kid3,))
    check("late /start re-stamps issued key chat",
          row and row[0] == "515151", str(row))

finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
