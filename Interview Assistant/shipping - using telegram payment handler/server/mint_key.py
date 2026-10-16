"""Mint license keys by hand — same signer the server uses.

Reads server/.env (SIGNING_PRIVATE_KEY required) and writes into the same
database the server uses (DATABASE_URL, or the local SQLite file).

  python mint_key.py                 mint a standard 30-day key
  python mint_key.py --days 90       mint a 90-day key
  python mint_key.py --note "comp"   tag the key
  python mint_key.py --genkey        generate a NEW keypair (update Render env
                                     + client public key after rotating!)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Existing env vars win."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(os.getenv("PERIOD_DAYS", "30")))
    ap.add_argument("--note", default="")
    ap.add_argument("--genkey", action="store_true",
                    help="generate a new Ed25519 keypair and exit")
    args = ap.parse_args()

    _load_dotenv(HERE / ".env")

    if args.genkey:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        import base64
        k = Ed25519PrivateKey.generate()
        priv = k.private_bytes(serialization.Encoding.Raw,
                               serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.Raw,
                                          serialization.PublicFormat.Raw)
        print("SIGNING_PRIVATE_KEY=" +
              base64.urlsafe_b64encode(priv).decode().rstrip("="))
        print("CLIENT_PUBLIC_KEY_HEX=" + pub.hex())
        print("\nSet SIGNING_PRIVATE_KEY in server/.env AND the Render dashboard,")
        print("and replace CLIENT_PUBLIC_KEY_HEX in the client's licensing module.")
        return 0

    # Import app AFTER env is loaded so config picks it up.
    sys.path.insert(0, str(HERE))
    import app as server_app  # noqa: E402
    import keysign  # noqa: E402

    if not os.getenv("SIGNING_PRIVATE_KEY"):
        print("SIGNING_PRIVATE_KEY not set (server/.env missing?)", file=sys.stderr)
        return 1
    server_app.init_db()
    priv = server_app.SIGNING or keysign.load_private_key()
    now = int(time.time())
    kid = keysign.new_kid()
    exp = now + args.days * 86400
    key_str = keysign.sign_key(priv, now, exp, kid)
    with server_app.get_db() as conn:
        cur = conn.cursor()
        cur.execute(server_app._sql(
            "INSERT INTO issued_keys(kid,key_str,order_id,note,iat,exp,revoked) "
            "VALUES(?,?,?,?,?,?,0)"), (kid, key_str, "", args.note, now, exp))
        cur.close()
    print(f"key   : {key_str}")
    print(f"kid   : {kid}")
    print(f"valid : {args.days} days (until {time.strftime('%Y-%m-%d', time.gmtime(exp))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
