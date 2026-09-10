"""Dual-sink event bus: overlay (local, fast) + Telegram (external, stealth).

Original pipeline in orchestrator.py writes only to TelegramSender.
GUI pipeline publishes every event to BOTH:
  - ui_queue (thread-safe, polled by Tk overlay)
  - TelegramSender (unchanged, phone fallback for screen-share interviews)

Nothing here modifies ../*.py — it only imports from them.
"""
from __future__ import annotations

import queue
from dataclasses import dataclass
from typing import Any


@dataclass
class UIEvent:
    kind: str  # partial | transcript | token | suggestion_done | status | error
    text: str = ""


class EventBus:
    def __init__(self) -> None:
        self.ui_queue: "queue.Queue[UIEvent]" = queue.Queue()

    # -- publishers (safe to call from any thread / async task) --
    def post(self, kind: str, text: str = "") -> None:
        try:
            self.ui_queue.put_nowait(UIEvent(kind, text))
        except queue.Full:
            pass

    def partial(self, text: str) -> None:
        self.post("partial", text)

    def transcript(self, text: str) -> None:
        self.post("transcript", text)

    def token(self, tok: str) -> None:
        self.post("token", tok)

    def suggestion_done(self, full: str) -> None:
        self.post("suggestion_done", full)

    def status(self, text: str) -> None:
        self.post("status", text)

    def error(self, text: str) -> None:
        self.post("error", text)

    # -- consumer (Tk side, called via root.after) --
    def drain(self) -> list[UIEvent]:
        out: list[UIEvent] = []
        while True:
            try:
                out.append(self.ui_queue.get_nowait())
            except queue.Empty:
                return out
