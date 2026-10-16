"""Offscreen smoke test for the PyQt6 license gate (client/license_window.py).

Run:  python tests/license_ui_smoke.py
Covers: window constructs with app styling, Activate success flow returns the
license triple and persists it, Quit returns None. No network, no real cache.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

import licensing                       # noqa: E402
import license_window                  # noqa: E402
from PyQt6.QtCore import QTimer        # noqa: E402
from PyQt6.QtWidgets import QLineEdit  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


FAKE_EXP = 4102444800  # 2100-01-01


def _fake_validate(key, cached=None):
    key = (key or "").strip()
    if key.startswith(licensing.KEY_PREFIX):
        return {"kid": "smoketest", "exp": FAKE_EXP, "v": 1, "iat": 1}, "ok"
    return None, "bad key"


def _install_fakes():
    licensing.validate_local = _fake_validate
    licensing.activate = lambda key, device_id, on_retry=None: (True, "activated", FAKE_EXP)


def run_gate_case(name, interact, expect):
    stored = {}
    result = license_window.run_gate(
        "smoke-device", "Enter key and Activate, or Buy to get one.",
        "", lambda k, kid, exp: stored.update(key=k, kid=kid, exp=exp))
    gate = license_window.run_gate.last_window
    # interact() is armed before exec() runs via the timer below; failure to
    # close within 15s is treated as a hang and force-fails the case.
    check(f"{name}: interacted", getattr(gate, "_interacted", False),
          "timer never fired")
    if expect is None:
        check(f"{name}: result is None (quit)", result is None, str(result))
    else:
        check(f"{name}: license returned", result is not None, str(result))
        if result:
            check(f"{name}: triple shape", result[2] == "smoke-device", str(result))
        check(f"{name}: store called", stored.get("kid") == "smoketest", str(stored))
    return gate


def main():
    print("smoke: start", flush=True)
    _install_fakes()
    # Own the QApplication before arming timers; run_gate reuses it exactly
    # like JarvisUI does after a real gate session. NOTE: the reference must
    # be held — PyQt6 destroys an unassigned QApplication instantly.
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    print("smoke: app ready", flush=True)

    # ── Case 1: construct + activate success ──
    def interact_activate():
        print("smoke: interact_activate fired", flush=True)
        gate = license_window.run_gate.last_window
        gate._interacted = True
        inputs = gate.findChildren(QLineEdit)
        check("activate: QLineEdit present", len(inputs) == 1, str(len(inputs)))
        check("activate: styled title", "Interview Assistant" in gate.windowTitle(),
              gate.windowTitle())
        inputs[0].setText(f"{licensing.KEY_PREFIX}smoke.fake-key")
        QTimer.singleShot(15000, gate.close)   # hang guard
        gate._act_btn.click()

    def arm_activate():
        print("smoke: arm_activate fired", flush=True)
        QTimer.singleShot(200, interact_activate)

    QTimer.singleShot(50, arm_activate)
    print("smoke: timer armed, entering gate", flush=True)
    gate = run_gate_case("activate", interact_activate, ("key", "kid", "dev"))
    print("smoke: case 1 gate returned", flush=True)
    check("activate: status turned green", "starting" in gate._status.text().lower()
          or gate.result is not None, gate._status.text())
    check("activate: buttons re-enabled or closed", gate._busy is False or gate.result is not None,
          f"busy={gate._busy}")

    # ── Case 2: empty key → hint, then Quit → None ──
    def interact_quit():
        gate = license_window.run_gate.last_window
        gate._interacted = True
        gate.do_activate()                      # empty input → hint, no crash
        check("quit: empty-key hint", "paste" in gate._status.text().lower(),
              gate._status.text())
        QTimer.singleShot(15000, gate.close)    # hang guard
        QTimer.singleShot(300, gate.close)

    def arm_quit():
        QTimer.singleShot(200, interact_quit)

    QTimer.singleShot(50, arm_quit)
    run_gate_case("quit", interact_quit, None)

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): " + ", ".join(FAILURES))
        sys.exit(1)
    print("FAILURES: none")


if __name__ == "__main__":
    main()
