"""Visual preview of the PyQt6 license gate — no network, no real license.

Run:  python tests/license_ui_preview.py
Closing the window quits. Buy shows the waiting state (invoice is fake);
typing any 3L3-... key and pressing Activate completes the happy path.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

import licensing                     # noqa: E402
import license_window                # noqa: E402

licensing.validate_local = lambda key, cached=None: (
    {"kid": "preview", "exp": 4102444800, "v": 1, "iat": 1}, "ok"
) if (key or "").strip().startswith(licensing.KEY_PREFIX) else (None, "bad key")
licensing.activate = lambda key, device_id, on_retry=None: (True, "activated", 4102444800)
licensing.create_invoice = lambda prev=None, device_id="", on_retry=None: (
    "ORD-preview", "https://example.invalid/pay",
    "https://t.me/elitesolutionsinterviewsbot?start=buy_ORD-preview")
licensing.order_status = lambda order_id: ("pending", None)
license_window.webbrowser = type("W", (), {"open": staticmethod(
    lambda url: print(f"[preview] would open: {url}"))})


def _noop_store(key, kid, exp):
    print(f"[preview] would store kid={kid} exp={exp} (cache untouched)")


if __name__ == "__main__":
    result = license_window.run_gate(
        "ae77bf6e3b39674c6e31645900970b11",
        "Enter key and Activate, or Buy to get one.",
        "", _noop_store)
    print(f"[preview] closed, result={result}")
