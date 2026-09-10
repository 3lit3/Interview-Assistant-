"""Desktop entry point — fresh folder, original ../*.py untouched.

Reuses: config, audio_capture, stt, llm, context, cv_loader, gate, telegram.
Adds: Tk device/CV picker, dual-sink pipeline (overlay + Telegram), stealth overlay.

Run:
  ..\\ass_env\\Scripts\\python.exe GUI\\app.py [--mic | --loopback] [--device N] [--cv path] [--no-telegram]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

# --- import original project modules without modifying them ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from sinks import EventBus  # noqa: E402
from overlay import OverlayWindow  # noqa: E402
from licensing import ensure_licensed, verify_with_server, REVERIFY_MINUTES  # noqa: E402

from audio_capture import AudioCapture  # noqa: E402
from config import load_config  # noqa: E402
from context import ContextManager  # noqa: E402
from cv_loader import build_system_prompt, load_cv  # noqa: E402
from gate import is_question  # noqa: E402
from llm import LLMClient  # noqa: E402
from telegram import TelegramSender  # noqa: E402

log = logging.getLogger("gui")


# ---------- GUI pickers (replace main.py CLI prompts) ----------
def pick_device_gui(default_mode: str | None = None):
    """Returns (device_index, echo_transcript) or (None, None) on cancel."""
    import sounddevice as sd

    devs = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0]

    result: dict = {}
    win = tk.Tk()
    win.title("Select input source")
    win.geometry("560x420")
    win.attributes("-topmost", True)

    tk.Label(win, text="Input device (system audio = Stereo Mix / VB-Cable input):").pack(pady=6)
    lb = tk.Listbox(win, width=80, height=12)
    for i, d in inputs:
        name = d["name"]
        tag = "  [loopback]" if any(
            k in name.lower() for k in ("stereo mix", "what u hear", "cable", "vb-audio", "virtual", "waveout", "point")
        ) else ""
        lb.insert("end", f"[{i}] {name}{tag}")
    lb.pack(fill="both", expand=True, padx=10)
    if inputs:
        lb.select_set(0)

    mode_var = tk.StringVar(value=default_mode or "mic")
    row = tk.Frame(win)
    row.pack(pady=6)
    tk.Radiobutton(row, text="Microphone", variable=mode_var, value="mic").pack(side="left", padx=10)
    tk.Radiobutton(row, text="System / Speaker audio", variable=mode_var, value="system").pack(side="left")

    def ok():
        try:
            sel = lb.get(lb.curselection()[0])
            idx = int(sel.split("]")[0].strip("["))
        except Exception:
            idx = None
        result["device"] = idx
        result["mode"] = mode_var.get()
        win.destroy()

    def cancel():
        win.destroy()

    btns = tk.Frame(win)
    btns.pack(pady=6)
    tk.Button(btns, text="Use selected", command=ok).pack(side="left", padx=10)
    tk.Button(btns, text="Cancel", command=cancel).pack(side="left")
    win.mainloop()
    if "device" not in result:
        return None, None
    echo = result["mode"] == "mic"
    return result["device"], echo


def pick_cv_gui():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Select CV (optional)",
        filetypes=[("CV", "*.pdf *.docx *.txt"), ("All", "*.*")],
    )
    root.destroy()
    return path or ""


# ---------- Dual-sink pipeline (overlay + Telegram) ----------
class GuiPipeline:
    """Same logic as orchestrator.Orchestrator but publishes to bus + telegram."""

    def __init__(self, cfg, bus: EventBus, use_telegram: bool):
        self.cfg = cfg
        self.bus = bus
        self.audio = AudioCapture(cfg)
        self.transcript_q: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self.partial_q: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        cv_text = load_cv(cfg.cv_path)
        system_prompt = build_system_prompt(cfg.system_prompt, cv_text)
        self.context = ContextManager(system_prompt, cfg.context_window)
        self.llm = LLMClient(cfg, self.context)
        self.telegram = TelegramSender(cfg) if use_telegram else None
        from stt import STTEngine

        self.stt = STTEngine(cfg, self.audio, self.transcript_q, self.partial_q)
        self._running = False
        self._msg_id = None

    def set_mode(self, mode: str):
        # interview -> gate on (questions only, like system-audio use case)
        # general  -> gate off (answer everything)
        self.cfg.question_gate = mode == "interview"
        self.bus.status(f"Mode: {mode} (question_gate={self.cfg.question_gate})")

    def set_context(self, extra: str):
        base = self.cfg.system_prompt.split("\n\nThe candidate's CV")[0]
        self.context.system = f"{base}\n\nAdditional live context: {extra}" if extra else base

    async def run(self):
        self._running = True
        await self.audio.start()
        self.bus.status("Loading STT model (first run downloads weights)…")
        if self.telegram:
            await self.telegram.notify("🟡 Assistant starting — loading STT model…")
        await asyncio.get_event_loop().run_in_executor(None, self.stt._load_model)
        mode = "microphone" if self.cfg.echo_transcript else "system audio"
        self.bus.status(f"Live — source: {mode}. Speak now. (Ctrl+H hides overlay)")
        if self.telegram:
            await self.telegram.notify(f"🟢 Assistant live — source: {mode}.")
        log.info("Pipeline live")
        tasks = [
            asyncio.create_task(self.stt.run()),
            asyncio.create_task(self._pipeline()),
            asyncio.create_task(self._partials()),
        ]
        try:
            await asyncio.gather(*tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await self.shutdown()

    async def _partials(self):
        while self._running:
            try:
                text = await asyncio.wait_for(self.partial_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if self.cfg.echo_transcript:
                self.bus.partial(text)
                if self._msg_id is None and self.telegram:
                    self._msg_id = await self.telegram.begin_transcript()
                if self.telegram and self._msg_id is not None:
                    await self.telegram.update_transcript(self._msg_id, f"🎙 {text}▌")

    async def _pipeline(self):
        while self._running:
            try:
                utt = await asyncio.wait_for(self.transcript_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            log.info("USER: %s", utt)
            self.bus.transcript(utt)
            if self.cfg.question_gate and not self.cfg.echo_transcript and not is_question(utt):
                log.info("Dropped (not a question)")
                continue
            try:
                if self.telegram:
                    if self.cfg.echo_transcript and self._msg_id is not None:
                        await self.telegram.update_transcript(self._msg_id, f"🎙 {utt}")
                        self._msg_id = None
                    else:
                        await self.telegram.send_user_turn(utt)
                # Consume LLM tokens ONCE, fan-out to overlay + telegram
                full = ""
                async for tok in self.llm.stream_reply(utt):
                    full += tok
                    self.bus.token(tok)
                self.bus.suggestion_done(full)
                if self.telegram and full.strip():
                    await self.telegram.notify(full[:4000])
            except Exception as exc:  # noqa: BLE001
                log.exception("handle failed: %s", exc)
                self.bus.error(str(exc))

    async def shutdown(self):
        self._running = False
        try:
            await self.audio.stop()
        except Exception:
            pass
        try:
            await self.llm.close()
        except Exception:
            pass
        if self.telegram:
            try:
                await self.telegram.notify("🔴 Assistant stopped.")
                await self.telegram.close()
            except Exception:
                pass


# ---------- main ----------
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Interview Assistant desktop (GUI folder)")
    p.add_argument("--device", type=int, default=None)
    p.add_argument("--loopback", action="store_true")
    p.add_argument("--mic", action="store_true")
    p.add_argument("--cv", type=str, default=None)
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--no-license", action="store_true", help="Dev only: skip paywall")
    args = p.parse_args()

    lic = None
    if not args.no_license:
        lic = ensure_licensed()
        if lic is None:
            print("License required ($9.99/mo). Exiting.")
            try:
                messagebox.showinfo("License required", "Active $9.99/mo subscription required.")
            except Exception:
                pass
            return
        lic_key, lic_hwid = lic
    else:
        lic_key, lic_hwid = "DEV", "DEV"

    cfg = load_config()
    if args.device is not None:
        cfg.device_index = args.device
    if args.cv:
        cfg.cv_path = args.cv

    default_mode = "system" if args.loopback else ("mic" if args.mic else None)
    if cfg.device_index is None:
        dev, echo = pick_device_gui(default_mode)
        if dev is None:
            print("Cancelled.")
            return
        cfg.device_index = dev
        cfg.echo_transcript = echo
    else:
        cfg.echo_transcript = not args.loopback

    if not cfg.cv_path:
        try:
            cv = pick_cv_gui()
            if cv:
                cfg.cv_path = cv
        except Exception:
            pass

    prompt_file = os.path.join(_PARENT, "system_prompt.txt")
    if os.path.exists(prompt_file):
        cfg.system_prompt = open(prompt_file, encoding="utf-8").read().strip()

    bus = EventBus()
    use_telegram = (not args.no_telegram) and bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
    if not use_telegram:
        bus.status("Telegram disabled (missing keys or --no-telegram) — overlay only.")
        print("Telegram disabled — overlay only.")

    pipe = GuiPipeline(cfg, bus, use_telegram)

    def license_watchdog():
        # Re-verify with server: kills pipeline if sub expires / key moved.
        import time as _t

        while pipe._running or True:
            _t.sleep(REVERIFY_MINUTES * 60)
            if args.no_license:
                continue
            try:
                ok, msg, _ = verify_with_server(lic_key, lic_hwid)
                if not ok:
                    bus.error(f"License revoked/expired: {msg}")
                    pipe._running = False
                    break
            except Exception:
                pass

    def runner():
        try:
            asyncio.run(pipe.run())
        except RuntimeError:
            pass

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    if not args.no_license:
        threading.Thread(target=license_watchdog, daemon=True).start()

    if not use_telegram:
        # surface key warning inside overlay too
        pass

    win = OverlayWindow(bus, on_mode_change=pipe.set_mode, on_context_change=pipe.set_context)
    try:
        win.run()
    finally:
        pipe._running = False


if __name__ == "__main__":
    main()
