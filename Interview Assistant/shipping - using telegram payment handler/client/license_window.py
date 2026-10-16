"""PyQt6 license gate window.

The view for the paywall. Styling mirrors ui.py (class C tokens, Courier New,
panel chrome) so the gate looks like the app it protects — and because it runs
BEFORE the main window exists, it creates the QApplication itself; JarvisUI
(ui.py:6023) picks up the same instance afterwards via QApplication.instance().

License logic (offline validation, HTTP, cache) stays in licensing.py — this
module is the window plus the Buy/Activate worker threads.
"""
from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

import licensing as _lic

# ── Design tokens (mirrored from ui.py:107 class C) ───────────────

BG        = "#00060a"
PANEL     = "#000d12"
BORDER    = "#0d3347"
BORDER_B  = "#1a5c7a"
PRI       = "#00d4ff"
PRI_DIM   = "#007a99"
PRI_GHO   = "#001f2e"
GREEN     = "#00ff88"
RED       = "#ff3355"
TEXT      = "#8ffcff"
TEXT_DIM  = "#3a8a9a"
TEXT_MED  = "#5ab8cc"

_LEVEL_COLOR = {"info": TEXT_MED, "ok": GREEN, "err": RED}


def _courier(size: int, bold: bool = False, spacing: float = 0.0) -> QFont:
    f = QFont("Courier New", size,
              QFont.Weight.Bold if bold else QFont.Weight.Normal)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


class LicenseGate(QWidget):
    status_msg = pyqtSignal(str, str)   # (text, level: info|ok|err)
    busy_changed = pyqtSignal(bool)
    key_filled = pyqtSignal(str)

    def __init__(self, hwid: str, startup_msg: str, initial_key: str, store):
        super().__init__()
        self._hwid = hwid
        self._store = store
        self._busy = False
        self._closed = False
        self.result: tuple | None = None

        self.setWindowTitle(f"Interview Assistant — ${_lic.PLAN_PRICE} / "
                            f"{_lic.PLAN_DAYS} days")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(600)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            LicenseGate {{
                background: {BG};
                border: 1px solid {BORDER_B};
                border-radius: 6px;
            }}
            QLabel {{ background: transparent; }}
        """)
        icon = Path(__file__).resolve().parent.parent / "face.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 24, 30, 20)
        layout.setSpacing(8)

        def _lbl(txt: str, size: int = 9, bold: bool = False,
                 color: str = TEXT, align=Qt.AlignmentFlag.AlignCenter,
                 spacing: float = 0.0) -> QLabel:
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(_courier(size, bold, spacing))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        def _sep() -> QFrame:
            s = QFrame()
            s.setFrameShape(QFrame.Shape.HLine)
            s.setStyleSheet(f"color: {BORDER};")
            return s

        # ── Header ──
        layout.addWidget(_lbl("◈  INTERVIEW ASSISTANT", 15, True, PRI,
                              spacing=1.5))
        layout.addWidget(_lbl(f"${_lic.PLAN_PRICE} / {_lic.PLAN_DAYS} DAYS"
                              f"  ·  UP TO {_lic.DEVICE_CAP} DEVICES",
                              9, True, TEXT))
        layout.addSpacing(4)
        layout.addWidget(_sep())
        layout.addSpacing(4)

        # ── Device ──
        layout.addWidget(_lbl("DEVICE ID", 8, False, TEXT_DIM,
                              align=Qt.AlignmentFlag.AlignLeft))
        dev = _lbl(self._hwid, 9, False, TEXT_MED,
                   align=Qt.AlignmentFlag.AlignLeft)
        dev.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(dev)

        # ── Key ──
        layout.addWidget(_lbl("LICENSE KEY", 8, False, TEXT_DIM,
                              align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("3L3-\u2026")
        self._key_input.setFont(_courier(10))
        self._key_input.setFixedHeight(34)
        self._key_input.setText(initial_key)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: {PANEL}; color: {TEXT};
                border: 1px solid {BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {PRI}; }}
        """)
        layout.addWidget(self._key_input)

        # ── Status ──
        self._status = QLabel(startup_msg)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)
        self._status.setMinimumHeight(36)
        self._status.setFont(_courier(9))
        self._status.setStyleSheet(f"color: {TEXT_MED}; background: transparent;")
        layout.addWidget(self._status)
        layout.addSpacing(4)

        # ── Buttons (centered row; styles copied from SetupOverlay) ──
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)

        self._buy_btn = QPushButton(f"BUY ${_lic.PLAN_PRICE}")
        self._buy_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {PRI};
                border: 1px solid {PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {PRI_GHO}; border: 1px solid {PRI};
            }}
            QPushButton:disabled {{ color: {TEXT_DIM}; border: 1px solid {BORDER}; }}
        """)

        self._act_btn = QPushButton("ACTIVATE")
        self._act_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PANEL}; color: {TEXT_DIM};
                border: 1px solid {BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {TEXT}; border: 1px solid {BORDER_B}; }}
            QPushButton:disabled {{ color: {TEXT_DIM}; border: 1px solid {BORDER}; }}
        """)

        quit_btn = QPushButton("QUIT")
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: 1px solid {BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {RED}; border: 1px solid {RED}; }}
        """)

        for b in (self._buy_btn, self._act_btn, quit_btn):
            b.setFont(_courier(9, True))
            b.setFixedHeight(34)
            b.setMinimumWidth(120)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

        # ── Footer ──
        layout.addWidget(_sep())
        footer = _lbl(
            f"Payment via NOWPayments (crypto). Renewal extends {_lic.PLAN_DAYS} days.\n"
            f"Key works on up to {_lic.DEVICE_CAP} devices. Buy opens Telegram so the\n"
            "key is delivered to your chat; it also appears on the payment\n"
            "success page and is auto-filled here.",
            7, False, TEXT_DIM)
        footer.setWordWrap(True)
        layout.addWidget(footer)

        # ── Wiring ──
        self.status_msg.connect(self._apply_status)
        self.busy_changed.connect(self._apply_busy)
        self.key_filled.connect(self._key_input.setText)
        self._buy_btn.clicked.connect(self.do_buy)
        self._act_btn.clicked.connect(self.do_activate)
        quit_btn.clicked.connect(self.close)

    # ── UI helpers (safe from worker threads via signals) ──────────

    def _apply_status(self, text: str, level: str) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(
            f"color: {_LEVEL_COLOR.get(level, TEXT_MED)}; background: transparent;")

    def _apply_busy(self, busy: bool) -> None:
        self._busy = busy
        self._buy_btn.setEnabled(not busy)
        self._act_btn.setEnabled(not busy)

    def _emit(self, text: str, level: str = "info") -> None:
        try:
            self.status_msg.emit(text, level)
        except RuntimeError:
            pass

    # ── Flows (ported 1:1 from the old Tk paywall) ────────────────

    def _finish(self, key: str, payload: dict, exp: int) -> None:
        kid = str(payload["kid"])
        self.result = (key, kid, self._hwid)
        try:
            self._store(key, kid, exp)
        except Exception:
            pass
        self._emit("License active — starting\u2026", "ok")
        QTimer.singleShot(500, self.close)

    def _try_activate(self, key: str) -> None:
        payload, why = _lic.validate_local(key)
        if not payload:
            self._emit(f"Not a valid key: {why}", "err")
            self.busy_changed.emit(False)
            return
        self._emit("Activating this device\u2026")

        def work():
            def on_retry(attempt, total, exc):
                self._emit(f"Server is waking up\u2026 retry {attempt + 1}/{total} (~60s).")
            ok, msg, exp = _lic.activate(key, self._hwid, on_retry=on_retry)
            if ok:
                self._finish(key, payload, exp)
            else:
                self._emit(msg, "err")
                self.busy_changed.emit(False)

        threading.Thread(target=work, daemon=True).start()

    def do_activate(self) -> None:
        key = self._key_input.text().strip()
        if not key:
            self._emit("Paste your license key first.")
            return
        self.busy_changed.emit(True)
        self._try_activate(key)

    def do_buy(self) -> None:
        self.busy_changed.emit(True)
        self._emit("Creating invoice\u2026 (server may need ~60s to wake if idle)")
        entry_key = self._key_input.text().strip()
        prev = entry_key if _lic.validate_local(entry_key)[0] else None

        def work():
            def on_retry(attempt, total, exc):
                self._emit(f"Server is waking up\u2026 retry {attempt + 1}/{total}. "
                           f"Leave this window open (~60s).")
            try:
                order_id, url, bot_start = _lic.create_invoice(
                    prev, self._hwid, on_retry=on_retry)
            except Exception as exc:
                self._emit(f"Buy failed: {_lic._friendly_net_error(exc)}", "err")
                self.busy_changed.emit(False)
                return
            # Prefer the bot deep link: the chat becomes the delivery channel
            # for the key. No bot configured -> plain browser invoice.
            try:
                webbrowser.open(bot_start or url)
            except Exception:
                pass
            if bot_start:
                self._emit(f"Telegram opened — tap Start there to receive the "
                           f"invoice (order {order_id}), pay, and the key comes "
                           f"back to this window and to the chat. Waiting\u2026")
            else:
                self._emit(f"Complete payment in the browser (order {order_id}). "
                           f"Waiting for confirmation\u2026")
            # Poll until the server mints the key (IPN can lag a little).
            for _ in range(240):          # 240 × 5s = 20 min
                for _ in range(50):       # 5s in 0.1s slices so a closed
                    if self._closed:      # window stops the loop at once
                        return
                    time.sleep(0.1)
                try:
                    st, key = _lic.order_status(order_id)
                except Exception:
                    continue
                if self._closed:
                    return
                if st == "issued" and key:
                    self.key_filled.emit(key)
                    self._emit("Payment received — activating this device\u2026")
                    self._try_activate(key)
                    return
            self._emit("Still waiting for payment — click Buy again to re-check, "
                       "or paste your key once you have it.")
            self.busy_changed.emit(False)

        threading.Thread(target=work, daemon=True).start()

    def closeEvent(self, ev) -> None:
        self._closed = True
        app = QApplication.instance()
        if app is not None:
            app.quit()   # guarantees run_gate's exec() returns
        super().closeEvent(ev)


def run_gate(hwid: str, startup_msg: str, initial_key: str,
             store) -> tuple | None:
    """Show the gate and block. Returns (key, kid, hwid) or None on quit.

    Reuses an existing QApplication when one is present (tests, embedders);
    otherwise creates the process-wide instance JarvisUI will later adopt."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    # PyQt6 does NOT keep the QApplication alive on its own (an unassigned
    # instance is destroyed immediately) — hold it for the whole process so
    # JarvisUI's `QApplication.instance() or ...` reuses THIS app after the
    # gate session ends.
    run_gate.app = app
    gate = LicenseGate(hwid, startup_msg, initial_key, store)
    # Keep the last gate alive past exec(): in-flight worker threads may
    # still emit into it for a moment after close.
    run_gate.last_window = gate
    gate.show()
    screen = app.primaryScreen()
    if screen is not None:
        ag = screen.availableGeometry()
        gate.move(ag.center().x() - gate.width() // 2,
                  ag.center().y() - gate.height() // 2)
    gate.raise_()
    gate.activateWindow()
    app.exec()
    return gate.result
