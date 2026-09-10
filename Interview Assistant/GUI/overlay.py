"""Stealth overlay window (tkinter, stdlib-only).

Why tkinter: works today in ass_env with zero new native deps.
Styling mirrors caption-assistant screenshot: Live Caption | Suggestion.

Stealth features (Windows):
  - always-on-top
  - WDA_EXCLUDEFROMCAPTURE -> invisible to Zoom/Teams/Meet screen share,
    still visible to you. (Win10 2004+)
  - panic hotkey Ctrl+H hides instantly, Ctrl+Shift+H shows
  - optional click-through toggle (WS_EX_TRANSPARENT)

All UI updates come from EventBus.drain() polled with root.after(),
so the asyncio audio pipeline can run in a background thread safely.
"""
from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import ttk

from sinks import EventBus

WDA_EXCLUDEFROMCAPTURE = 0x11


def _set_excluded_from_capture(root: tk.Tk, exclude: bool) -> tuple[bool, str]:
    """Hide window from screen-capture/share. Returns (ok, detail)."""
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
        # HWND only valid after mapping — force it.
        try:
            root.update_idletasks()
        except Exception:
            pass
        hwnd = root.winfo_id()
        affinity = WDA_EXCLUDEFROMCAPTURE if exclude else 0
        ok = bool(user32.SetWindowDisplayAffinity(hwnd, affinity))
        if ok:
            return True, f"hwnd={hwnd:#x}"
        err = ctypes.windll.kernel32.GetLastError()
        # Retry on parent handle (some Tk builds nest the top-level).
        try:
            parent = user32.GetParent(hwnd)
            if parent and parent != hwnd:
                ok2 = bool(user32.SetWindowDisplayAffinity(parent, affinity))
                if ok2:
                    return True, f"parent hwnd={parent:#x}"
        except Exception:
            pass
        return False, f"SetWindowDisplayAffinity failed hwnd={hwnd:#x} err={err}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _set_click_through(root: tk.Tk, enable: bool) -> None:
    try:
        hwnd = root.winfo_id()
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x20
        WS_EX_LAYERED = 0x80000
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enable:
            style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
        else:
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


class OverlayWindow:
    def __init__(self, bus: EventBus, on_mode_change=None, on_context_change=None):
        self.bus = bus
        self.on_mode_change = on_mode_change
        self.on_context_change = on_context_change
        self._suggestion_buf = ""
        self._captions: list[str] = []
        self._hidden_panic = False

        self.root = tk.Tk()
        self.root.title("Interview Assistant (local overlay)")
        self.root.geometry("900x560")
        self.root.attributes("-topmost", True)
        # NOTE: -alpha < 1.0 forces a layered window which breaks
        # WDA_EXCLUDEFROMCAPTURE on some Windows builds. Keep opaque
        # until exclusion is confirmed; user can lower opacity after.

        self.excluded = tk.BooleanVar(value=True)
        self.click_through = tk.BooleanVar(value=False)
        self.mode = tk.StringVar(value="interview")

        self._build()
        self._bind_hotkeys()
        self.root.after(120, self._poll)
        # Apply exclusion AFTER the HWND exists (mapped). Retry twice:
        # some capture stacks reset affinity on map/show.
        self.root.after(300, self._apply_exclusion)
        self.root.after(1500, self._apply_exclusion)

    # -- layout (mirrors reference screenshot) --
    def _build(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Radiobutton(top, text="Interview Mode", variable=self.mode,
                        value="interview", command=self._mode_changed).pack(side="left")
        ttk.Radiobutton(top, text="General Mode", variable=self.mode,
                        value="general", command=self._mode_changed).pack(side="left", padx=8)

        self.status_var = tk.StringVar(value="Idle — pick device, press Start (in console/GUI controls).")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=12)

        ttk.Checkbutton(top, text="Exclude from share", variable=self.excluded,
                        command=self._apply_exclusion).pack(side="right")
        ttk.Checkbutton(top, text="Click-through", variable=self.click_through,
                        command=self._apply_click_through).pack(side="right", padx=8)
        ttk.Button(top, text="Hide (Ctrl+H)", command=self.panic_hide).pack(side="right", padx=4)

        mid = ttk.Frame(self.root, padding=(8, 0, 8, 0))
        mid.pack(fill="both", expand=True)

        left = ttk.LabelFrame(mid, text="Live Caption", padding=6)
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.caption_box = tk.Text(left, height=16, wrap="word", state="disabled")
        self.caption_box.pack(fill="both", expand=True)
        self.partial_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.partial_var, foreground="gray").pack(fill="x")

        right = ttk.LabelFrame(mid, text="Suggestion (local + Telegram mirror)", padding=6)
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self.suggest_box = tk.Text(right, height=16, wrap="word", state="disabled")
        self.suggest_box.pack(fill="both", expand=True)

        bot = ttk.LabelFrame(self.root, text="Context / topic (tailors suggestions)", padding=6)
        bot.pack(fill="x", padx=8, pady=8)
        self.context_box = tk.Text(bot, height=3, wrap="word")
        self.context_box.pack(fill="x")
        self.context_box.bind("<KeyRelease>", self._context_changed)

    # -- win32 stealth --
    def _apply_exclusion(self):
        ok, detail = _set_excluded_from_capture(self.root, self.excluded.get())
        if ok and self.excluded.get():
            self._note(f"Share-exclusion ON ({detail}) — hidden from screen share.")
        elif not self.excluded.get():
            self._note("Share-exclusion OFF by user — WILL show on share. Use Telegram phone fallback.")
        else:
            self._note(f"Share-exclusion FAILED ({detail}). Win10 2004+ required. Use Telegram phone fallback.")
        # Re-assert after withdraw/deiconify — affinity is lost on recreate.
        return ok

    def _apply_click_through(self):
        _set_click_through(self.root, self.click_through.get())

    def _bind_hotkeys(self):
        self.root.bind("<Control-h>", lambda e: self.panic_hide())
        self.root.bind("<Control-H>", lambda e: self.panic_hide())
        self.root.bind("<Control-Shift-H>", lambda e: self.panic_show())

    def panic_hide(self):
        self._hidden_panic = True
        self.root.withdraw()

    def panic_show(self):
        self._hidden_panic = False
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.after(300, self._apply_exclusion)

    # -- callbacks --
    def _mode_changed(self):
        if self.on_mode_change:
            self.on_mode_change(self.mode.get())

    def _context_changed(self, _evt=None):
        if self.on_context_change:
            self.on_context_change(self.context_box.get("1.0", "end").strip())

    # -- event pump --
    def _note(self, msg: str):
        self.status_var.set(msg)

    def _append(self, widget: tk.Text, text: str):
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _poll(self):
        for ev in self.bus.drain():
            if ev.kind == "status":
                self._note(ev.text)
            elif ev.kind == "error":
                self._note(f"Error: {ev.text}")
            elif ev.kind == "partial":
                self.partial_var.set(f"🎙 {ev.text}▌")
            elif ev.kind == "transcript":
                self.partial_var.set("")
                self._captions.append(ev.text)
                self._append(self.caption_box, f"🎙 {ev.text}\n")
            elif ev.kind == "token":
                self._suggestion_buf += ev.text
                self._append(self.suggest_box, ev.text)
            elif ev.kind == "suggestion_done":
                if not self._suggestion_buf.strip() and ev.text.strip():
                    self._append(self.suggest_box, ev.text)
                self._append(self.suggest_box, "\n\n---\n")
                self._suggestion_buf = ""
        if not self._hidden_panic:
            try:
                # re-assert topmost (some sharers steal z-order)
                self.root.attributes("-topmost", True)
            except Exception:
                pass
        self.root.after(120, self._poll)

    def run(self):
        self.root.mainloop()
