"""Desktop entry point — fresh folder, original ../*.py untouched.

Reuses: config, audio_capture, stt, llm, context, cv_loader, gate, telegram.
Adds: Tk device/CV picker, dual-sink pipeline (overlay + Telegram), stealth overlay.

Modified from GUI/app.py:
  - Calls /issue-key at startup to get encrypted LLM API key.
  - Sets cfg.llm_api_key from decrypted key (no .env key needed).
  - Key held in memory only, wiped on exit.

Run:
  ..\\ass_env\\Scripts\\python.exe client\\app.py [--mic | --loopback] [--device N] [--cv path] [--no-telegram]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
_SHARED = os.path.join(_PARENT, "shared")

for p in (_PARENT, _HERE, _SHARED):
    if p not in sys.path:
        sys.path.insert(0, p)

from sinks import EventBus
from overlay import OverlayWindow
from licensing import ensure_licensed, issue_llm_key, verify_with_server, REVERIFY_MINUTES
from telegram_setup import load_user_env, prompt_telegram_deep_link, check_telegram

from audio_capture import AudioCapture
from config import load_config
from context import ContextManager
from cv_loader import build_system_prompt, load_cv
from gate import is_question
from llm import LLMClient
from telegram import TelegramSender

log = logging.getLogger("gui")


_SKIP_ENV_KEYS = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}


def _load_dotenv(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh.read().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                k = key.strip()
                if k in _SKIP_ENV_KEYS:
                    continue
                os.environ.setdefault(k, val.strip().strip('"').strip("'"))
    except OSError:
        pass


def _bootstrap_frozen_env() -> None:
    if getattr(sys, "frozen", False):
        _load_dotenv(os.path.join(os.path.dirname(sys.executable), ".env"))


# ── Model picker + download progress ─────────────────────────────

STT_MODELS = [
    {"id": "tiny.en",  "size": "75 MB",  "speed": "Fastest", "accuracy": "Poor",    "desc": "Quick but misses words often"},
    {"id": "base.en",  "size": "142 MB", "speed": "Fast",    "accuracy": "Decent",  "desc": "Good balance for real-time use"},
    {"id": "small.en", "size": "466 MB", "speed": "Medium",  "accuracy": "Good",    "desc": "Best accuracy, slight delay"},
]


def pick_model_gui(current: str = "base.en") -> str | None:
    """Model picker dialog. Returns selected model id or None on cancel."""
    result: dict = {}
    win = tk.Tk()
    win.title("Select STT Model")
    win.geometry("520x400")
    win.attributes("-topmost", True)

    ttk.Label(win, text="Speech Recognition Model", font=("Segoe UI", 14, "bold")).pack(pady=(12, 4))
    ttk.Label(win, text="Choose based on your priority: speed vs accuracy.\n"
              "First use downloads the model (shown below). Subsequent runs are instant.",
              wraplength=460).pack()

    selected = tk.StringVar(value=current)
    for m in STT_MODELS:
        frame = ttk.LabelFrame(win, padding=8)
        frame.pack(fill="x", padx=12, pady=4)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Radiobutton(row, text=m["id"], variable=selected, value=m["id"]).pack(side="left")
        ttk.Label(row, text=f'{m["size"]}  |  {m["speed"]}  |  {m["accuracy"]}',
                  foreground="gray").pack(side="right")
        ttk.Label(frame, text=m["desc"], foreground="gray").pack(anchor="w")

    status = tk.StringVar(value="")
    progress = ttk.Progressbar(win, mode="determinate", length=440)
    progress.pack(pady=(8, 2))
    ttk.Label(win, textvariable=status, foreground="gray").pack()

    def ok():
        result["model"] = selected.get()
        win.destroy()

    def cancel():
        win.destroy()

    btns = ttk.Frame(win)
    btns.pack(pady=8)
    ttk.Button(btns, text="Download & Use", command=ok).pack(side="left", padx=6)
    ttk.Button(btns, text="Cancel", command=cancel).pack(side="left", padx=6)
    win.protocol("WM_DELETE_WINDOW", cancel)
    win.mainloop()
    return result.get("model")


def download_model_with_progress(model_id: str, progress_bar, status_var, win) -> bool:
    """Download model files with progress tracking. Returns True on success."""
    from huggingface_hub import snapshot_download, hf_hub_url
    import requests

    repo_id = f"Systran/faster-whisper-{model_id}"
    cache_dir = None

    status_var.set(f"Checking {model_id} model files...")
    progress_bar["value"] = 0
    win.update()

    try:
        # Get file list first
        api_url = f"https://huggingface.co/api/models/{repo_id}/tree/main"
        resp = requests.get(api_url, timeout=15)
        files = [f["path"] for f in resp.json() if f.get("type") == "file"]

        if not files:
            status_var.set("Could not fetch model file list.")
            win.update()
            return False

        total = len(files)
        downloaded = 0

        for fname in files:
            status_var.set(f"Downloading {fname} ({downloaded+1}/{total})...")
            progress_bar["value"] = (downloaded / total) * 100
            win.update()

            # snapshot_download handles caching — skips if already downloaded
            snapshot_download(repo_id, allow_patterns=[fname], tqdm_class=None)
            downloaded += 1
            progress_bar["value"] = (downloaded / total) * 100
            win.update()

        status_var.set(f"{model_id} ready!")
        progress_bar["value"] = 100
        win.update()
        return True
    except Exception as exc:
        status_var.set(f"Download failed: {exc}")
        win.update()
        return False


def ensure_model_downloaded(model_id: str) -> bool:
    """Check if model is cached. If not, show download dialog."""
    from huggingface_hub import try_to_load_from_cache
    import os as _os

    repo_id = f"Systran/faster-whisper-{model_id}"
    # Check if model file is cached
    cache_path = try_to_load_from_cache(repo_id, "model.bin")
    if cache_path is not None and not isinstance(cache_path, type(...)):
        return True  # already cached

    # Not cached — show download dialog
    win = tk.Tk()
    win.title(f"Download {model_id}")
    win.geometry("500x200")
    win.attributes("-topmost", True)

    ttk.Label(win, text=f"Downloading {model_id} model...",
              font=("Segoe UI", 12, "bold")).pack(pady=(16, 8))
    progress = ttk.Progressbar(win, mode="determinate", length=420)
    progress.pack(pady=4, padx=16)
    status = tk.StringVar(value="Starting download...")
    ttk.Label(win, textvariable=status, foreground="gray").pack(pady=4)

    ok = download_model_with_progress(model_id, progress, status, win)
    if ok:
        win.after(600, win.destroy)
        win.mainloop()
    else:
        ttk.Button(win, text="OK", command=win.destroy).pack(pady=8)
        win.mainloop()
    return ok


def pick_device_gui(default_mode: str | None = None):
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


class GuiPipeline:
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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []

    def set_mode(self, mode: str):
        self.cfg.question_gate = mode == "interview"
        self.bus.status(f"Mode: {mode} (question_gate={self.cfg.question_gate})")

    def set_context(self, extra: str):
        base = self.cfg.system_prompt.split("\n\nThe candidate's CV")[0]
        self.context.system = f"{base}\n\nAdditional live context: {extra}" if extra else base

    async def run(self):
        self._running = True
        self._loop = asyncio.get_running_loop()
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
        self._tasks = [
            asyncio.create_task(self.stt.run()),
            asyncio.create_task(self._pipeline()),
            asyncio.create_task(self._partials()),
        ]
        try:
            await asyncio.gather(*self._tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await self.shutdown()

    def request_stop(self):
        self._running = False
        loop = self._loop
        if loop is not None:
            def _cancel():
                for t in self._tasks:
                    t.cancel()
            try:
                loop.call_soon_threadsafe(_cancel)
            except RuntimeError:
                pass

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
                full = ""
                async for tok in self.llm.stream_reply(utt):
                    full += tok
                    self.bus.token(tok)
                self.bus.suggestion_done(full)
                if self.telegram and full.strip():
                    await self.telegram.notify(full[:4000])
            except Exception as exc:
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


def main():
    _bootstrap_frozen_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import pathlib as _pl
    _logdir = _pl.Path(os.getenv("APPDATA") or _pl.Path.home()) / "InterviewAssistant"
    try:
        _logdir.mkdir(parents=True, exist_ok=True)
        _file = logging.FileHandler(str(_logdir / "app.log"))
        _file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(_file)
    except Exception:
        pass
    p = argparse.ArgumentParser(description="Interview Assistant desktop (shipping build)")
    p.add_argument("--device", type=int, default=None)
    p.add_argument("--loopback", action="store_true")
    p.add_argument("--mic", action="store_true")
    p.add_argument("--cv", type=str, default=None)
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--no-license", action="store_true", help="Dev only: skip paywall + key issuance")
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
    load_user_env()
    cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cfg.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    # --- Key issuance: get encrypted LLM key from server ---
    if not args.no_license:
        try:
            api_key = issue_llm_key(lic_key, lic_hwid)
            cfg.llm_api_key = api_key
            log.info("LLM key issued and decrypted successfully")
        except Exception as exc:
            log.error("Key issuance failed: %s", exc)
            try:
                messagebox.showerror("Key Error",
                    f"Failed to get LLM key from server:\n{exc}\n\n"
                    "Check your internet connection and try again.")
            except Exception:
                pass
            return
    else:
        pass

    if args.device is not None:
        cfg.device_index = args.device
    if args.cv:
        cfg.cv_path = args.cv

    # --- Model picker + ensure downloaded ---
    chosen_model = pick_model_gui(cfg.stt_model)
    if chosen_model is None:
        print("Cancelled.")
        return
    cfg.stt_model = chosen_model
    if not ensure_model_downloaded(chosen_model):
        print("Model download failed.")
        return

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

    # --- Telegram: deep-link auto-connect (no manual token/ID entry) ---
    from licensing import server_url as _srv
    if not args.no_telegram and not cfg.telegram_chat_id:
        action, chat_id, bot_token = prompt_telegram_deep_link(_srv(), lic_key, lic_hwid)
        if action == "connected" and chat_id:
            cfg.telegram_chat_id = chat_id
            if bot_token:
                cfg.telegram_bot_token = bot_token

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
    def telegram_enabled() -> bool:
        return (not args.no_telegram) and bool(cfg.telegram_chat_id)

    if not telegram_enabled():
        bus.status("Telegram disabled (no chat ID connected) — overlay only.")
        print("Telegram disabled — overlay only.")

    pipe_holder: dict = {"pipe": None, "thread": None}

    def start_session():
        if pipe_holder["pipe"] is not None:
            return
        pipe = GuiPipeline(cfg, bus, telegram_enabled())
        pipe_holder["pipe"] = pipe

        def runner():
            try:
                asyncio.run(pipe.run())
            except RuntimeError:
                pass
            finally:
                pipe_holder["pipe"] = None
                try:
                    win.set_recording(False)
                except Exception:
                    pass
                bus.status("Session ended. Press Start for a new recording.")

        try:
            pipe.set_mode(win.current_mode())
        except Exception:
            pass
        try:
            pipe.set_context(win.current_context())
        except Exception:
            pass
        bus.status("Session starting — loading STT model if needed…")
        win.set_recording(True)
        t = threading.Thread(target=runner, daemon=True)
        pipe_holder["thread"] = t
        t.start()

    def stop_session():
        pipe = pipe_holder["pipe"]
        if pipe is None:
            return
        bus.status("Ending session…")
        pipe.request_stop()

    def license_watchdog():
        import time as _t
        while True:
            _t.sleep(REVERIFY_MINUTES * 60)
            if args.no_license:
                continue
            try:
                ok, msg, _ = verify_with_server(lic_key, lic_hwid)
                if not ok:
                    bus.error(f"License revoked/expired: {msg}")
                    pipe = pipe_holder["pipe"]
                    if pipe is not None:
                        pipe.request_stop()
                    break
            except Exception:
                pass

    def on_mode_change(mode: str):
        pipe = pipe_holder["pipe"]
        if pipe is not None:
            pipe.set_mode(mode)

    def on_context_change(extra: str):
        pipe = pipe_holder["pipe"]
        if pipe is not None:
            pipe.set_context(extra)

    def open_telegram_settings():
        from licensing import server_url as _srv
        if cfg.telegram_chat_id:
            bus.status(f"Telegram connected (chat: {cfg.telegram_chat_id}). "
                       "Reconnect? Opening deep link...")
        action, chat_id, bot_token = prompt_telegram_deep_link(_srv(), lic_key, lic_hwid)
        if action != "connected" or not chat_id:
            if not cfg.telegram_chat_id:
                bus.status("Telegram not connected — overlay only.")
            return
        cfg.telegram_chat_id = chat_id
        if bot_token:
            cfg.telegram_bot_token = bot_token
        pipe = pipe_holder["pipe"]
        if pipe is None or args.no_telegram:
            bus.status(f"Telegram connected (chat: {chat_id}). Applies to next recording.")
            return
        old = pipe.telegram
        pipe.telegram = TelegramSender(cfg) if chat_id else None
        pipe._msg_id = None
        if old is not None and pipe._loop is not None:
            try:
                pipe._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(old.close()))
            except RuntimeError:
                pass
        if pipe.telegram is not None and pipe._loop is not None:
            async def _hello():
                try:
                    await pipe.telegram.notify("🔔 Interview Assistant connected.")
                except Exception:
                    pass
            try:
                asyncio.run_coroutine_threadsafe(_hello(), pipe._loop)
            except RuntimeError:
                pass
            bus.status("Telegram connected — confirmation sent.")
        else:
            bus.status("Telegram connected for next session.")

    if not args.no_license:
        threading.Thread(target=license_watchdog, daemon=True).start()

    win = OverlayWindow(bus, on_mode_change=on_mode_change,
                        on_context_change=on_context_change,
                        on_start=start_session, on_end=stop_session,
                        on_telegram_settings=open_telegram_settings)
    bus.status("Idle — press Start to begin recording.")
    try:
        win.run()
    finally:
        pipe = pipe_holder["pipe"]
        if pipe is not None:
            pipe.request_stop()


if __name__ == "__main__":
    main()
