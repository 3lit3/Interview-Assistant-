"""Telegram delivery bot for the license server — stdlib only (no new deps).

One daemon thread long-polls getUpdates; the IPN/mint thread calls
send_message() to push the purchased key back to the paying chat.

Flow:
  client Buy  ->  opens https://t.me/<bot>?start=buy_<order_id>
  /start buy_<order_id>  ->  chat is linked to the order, bot replies with
  the NOWPayments invoice URL button; payment -> IPN -> mint -> key + links
  are pushed into that chat.  /mykeys lists the keys tied to a chat.

Database access is injected by app.py via start(token, hooks=...) so this
module never imports the FastAPI app (and can be imported standalone).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

API_BASE = "https://api.telegram.org"
MAX_MSG = 4000

_token = ""
_username = ""
_hooks: dict = {}
_offset = 0
_thread: "threading.Thread | None" = None

# Hooks (set by app.py) — all optional, all called without the GIL held:
#   lookup_order(order_id) -> {"prev_key": str} | None
#   key_for_order(order_id) -> (key_str, exp) | None
#   invoice_url(order_id, prev_key) -> str        ("" = could not create)
#   link_chat(order_id, chat_id) -> None
#   keys_by_chat(chat_id) -> [ {"kid", "key_str", "exp", "revoked"} ]
#   link_nonce(nonce, chat_id) -> bool            (app links chat -> license)


def _log(msg: str) -> None:
    print(f"[tgpay] {msg}", flush=True)


def _api(method: str, payload: dict | None = None, timeout: int = 45) -> dict:
    """POST to the Bot API. Never raises — failures come back as a dict."""
    if not _token:
        return {"ok": False, "error_code": 0, "description": "bot not configured"}
    data = json.dumps(payload if payload is not None else {}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/bot{_token}/{method}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"ok": False, "error_code": exc.code, "description": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error_code": 0, "description": str(exc)}


def start(token: str, hooks: dict) -> bool:
    """Validate the token (getMe) and spawn the long-poll daemon thread."""
    global _token, _hooks, _thread, _username
    token = (token or "").strip()
    if not token:
        return False
    _token = token
    _hooks = dict(hooks or {})
    me = _api("getMe", {}, timeout=20)
    if not me.get("ok"):
        _log(f"getMe failed: {me.get('description')}")
        _token = ""
        return False
    _username = str((me.get("result") or {}).get("username") or "")
    _thread = threading.Thread(target=_loop, name="tgpay", daemon=True)
    _thread.start()
    _log(f"bot @{_username} long-polling getUpdates")
    return True


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def bot_username() -> str:
    return _username


def deep_link(order_id: str) -> str:
    """"https://t.me/<bot>?start=buy_<order_id>" — "" when no bot is running."""
    if not _username or not order_id:
        return ""
    return f"https://t.me/{_username}?start=buy_{order_id}"


def send_message(chat_id, text: str, button: "tuple[str, str] | None" = None):
    """Push a message (IPN thread -> buyer's chat). Long text is chunked.
    Returns the FIRST message's message_id (so live edits can follow), True
    when all chunks landed without an id, or False on failure."""
    if not _token or chat_id in (None, ""):
        return False
    chunks = [c for c in (text or "").split("\n\n")] or [""]
    # Re-pack into <= MAX_MSG pieces without breaking a single line apart.
    packed: list[str] = []
    cur = ""
    for part in chunks:
        cand = part if not cur else cur + "\n\n" + part
        if len(cand) <= MAX_MSG:
            cur = cand
        else:
            if cur:
                packed.append(cur)
            while len(part) > MAX_MSG:
                packed.append(part[:MAX_MSG])
                part = part[MAX_MSG:]
            cur = part
    if cur:
        packed.append(cur)
    ok = True
    first_id: "int | bool" = True
    for i, piece in enumerate(packed):
        payload: dict = {"chat_id": chat_id, "text": piece}
        if i == 0 and button:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": button[0][:64], "url": button[1]}]]}
        res = _api("sendMessage", payload, timeout=30)
        if not res.get("ok"):
            ok = False
            _log(f"sendMessage to {chat_id} failed: {res.get('description')}")
        elif i == 0:
            try:
                first_id = int(res["result"]["message_id"])
            except Exception:
                first_id = True
    if not ok:
        return False
    return first_id


def edit_message(chat_id, message_id, text: str) -> bool:
    """Rewrite an already-sent message (live answer growth). False = failed."""
    if not _token or chat_id in (None, "") or message_id in (None, ""):
        return False
    res = _api("editMessageText",
               {"chat_id": chat_id, "message_id": int(message_id),
                "text": (text or "")[:MAX_MSG]}, timeout=30)
    if not res.get("ok"):
        _log(f"editMessageText {message_id} -> {chat_id} failed: {res.get('description')}")
        return False
    return True


def delete_message(chat_id, message_id) -> bool:
    """Retract a message (interviewer cut in). False = failed."""
    if not _token or chat_id in (None, "") or message_id in (None, ""):
        return False
    res = _api("deleteMessage",
               {"chat_id": chat_id, "message_id": int(message_id)}, timeout=30)
    if not res.get("ok"):
        _log(f"deleteMessage {message_id} -> {chat_id} failed: {res.get('description')}")
        return False
    return True


# ── Long polling ──────────────────────────────────────────────────

def _loop() -> None:
    global _offset
    while True:
        res = _api("getUpdates",
                   {"offset": _offset, "timeout": 30, "allowed_updates": ["message"]},
                   timeout=60)
        if not res.get("ok"):
            code, desc = res.get("error_code"), res.get("description", "")
            if code == 409:
                # Something else (the jarvis settings dialog) is polling the
                # same bot — Telegram only allows one getUpdates consumer.
                _log(f"409 conflict ({desc}) — another poller holds this bot; retrying")
                time.sleep(15)
            else:
                _log(f"getUpdates error ({code}): {desc}")
                time.sleep(5)
            continue
        for upd in res.get("result", []):
            try:
                _offset = int(upd.get("update_id", 0)) + 1
                _handle(upd)
            except Exception as exc:
                _log(f"update handler failed: {exc!r}")


# ── Command handling ──────────────────────────────────────────────

_HELP = ("Interview Assistant license bot.\n\n"
         "Buy: open the app and press Buy — it opens this chat with your order.\n"
         "You can also send:  /start buy_<order_id>\n\n"
         "/mykeys — list the license keys tied to this chat\n"
         "/help — this message")


def _handle(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = str(msg.get("text") or "").strip()
    if chat_id in (None, "") or not text:
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "start"):
        if arg.startswith("buy_"):
            _handle_buy(chat_id, arg[4:])
        elif arg.startswith("link_"):
            _handle_link(chat_id, arg[5:])
        else:
            send_message(chat_id, _HELP)
    elif cmd in ("/mykeys", "mykeys"):
        _handle_mykeys(chat_id)
    else:
        send_message(chat_id, _HELP)


def _handle_buy(chat_id, order_id: str) -> None:
    order_id = (order_id or "").strip()[:64]
    lookup = _hooks.get("lookup_order")
    key_for_order = _hooks.get("key_for_order")
    link_chat = _hooks.get("link_chat")

    # Already paid: hand the key over immediately (also stamps chat_id).
    row = key_for_order(order_id) if (key_for_order and order_id) else None
    if row:
        if link_chat:
            link_chat(order_id, chat_id)
        _send_key(chat_id, str(row[0]), int(row[1]), order_id)
        return

    order = lookup(order_id) if lookup else None
    if not order:
        send_message(chat_id,
                     f"I don't know order {order_id}.\n"
                     "Open the Interview Assistant app and press Buy — it will "
                     "bring you back here with a fresh order.")
        return
    if link_chat:
        link_chat(order_id, chat_id)

    prev_key = str((order or {}).get("prev_key") or "")
    url_fn = _hooks.get("invoice_url")
    url = url_fn(order_id, prev_key) if url_fn else ""
    if not url:
        send_message(chat_id,
                     f"Order {order_id}: the payment invoice could not be created "
                     "right now. Try again in a minute (or press Buy in the app).")
        return
    send_message(
        chat_id,
        f"Interview Assistant license — $14.99 / 30 days (up to 2 devices)\n\n"
        f"Order: {order_id}\n"
        "Pay with the button below. Your key is delivered to this chat as soon "
        "as the payment confirms.",
        button=("Open invoice", url))


def _handle_link(chat_id, nonce: str) -> None:
    """/start link_<nonce> — the app opened this deep link; confirm the chat
    is now the delivery target for the license that requested the code."""
    fn = _hooks.get("link_nonce")
    nonce = (nonce or "").strip()[:64]
    if not fn or not nonce:
        send_message(chat_id, _HELP)
        return
    if fn(nonce, chat_id):
        send_message(chat_id,
                     "Linked.\n\nThis chat is now the delivery chat for your "
                     "Interview Assistant license. Go back to the app — it "
                     "confirms in a moment.")
    else:
        send_message(chat_id,
                     "That link code is unknown or expired.\n\nIn the app, "
                     "press “Link chat from payment” again for a fresh one.")


def _send_key(chat_id, key_str: str, exp: int, order_id: str = "") -> None:
    base = _hooks.get("public_base", "") if isinstance(_hooks, dict) else ""
    when = time.strftime("%Y-%m-%d", time.gmtime(int(exp))) if exp else "—"
    text = ("Payment received.\n\n"
            f"Your license key (valid until {when}):\n\n"
            f"{key_str}\n\n"
            f"Redeem: myapp://license?key={key_str}")
    if base and order_id:
        text += f"\nPayment page: {base}/success/{order_id}"
    button = ("Open key page", f"{base}/success/{order_id}") if base and order_id else None
    send_message(chat_id, text, button=button)


def _handle_mykeys(chat_id) -> None:
    fn = _hooks.get("keys_by_chat")
    rows = fn(chat_id) if fn else []
    if not rows:
        send_message(chat_id,
                     "No keys are tied to this chat yet.\n"
                     "Buy in the app (or send /start buy_<order_id>) and the key "
                     "will be delivered here.")
        return
    lines = [f"Keys tied to this chat ({len(rows)}):"]
    for i, r in enumerate(rows[:10], 1):
        when = time.strftime("%Y-%m-%d", time.gmtime(int(r["exp"]))) if r.get("exp") else "—"
        state = "REVOKED" if r.get("revoked") else "active"
        lines.append(f"\n{i}) expires {when} [{state}]\n{r['key_str']}")
    if len(rows) > 10:
        lines.append(f"\n… {len(rows) - 10} older keys not shown.")
    send_message(chat_id, "\n\n".join(lines))
