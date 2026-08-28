import argparse
import asyncio
import logging
import os

import sounddevice as sd

from audio_capture import AudioCapture
from config import Config, load_config
from context import ContextManager
from cv_loader import build_system_prompt, load_cv
from llm import LLMClient
from orchestrator import Orchestrator
from stt import STTEngine
from telegram import TelegramSender

log = logging.getLogger("main")


def _default_device(which: str):
    d = sd.default.device
    if isinstance(d, (tuple, list)):
        return d[0] if which == "in" else d[1]
    return d


def _first_loopback_device():
    devs = sd.query_devices()
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0:
            n = d["name"].lower()
            if any(k in n for k in ("stereo mix", "what u hear", "cable", "vb-audio", "virtual", "waveout", "point")):
                return i
    return None


def choose_input_source(cfg: Config, default_mode: str | None = None) -> Config:
    devs = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
    print("\nInput-capable devices:")
    for i, d in inputs:
        name = d["name"].lower()
        tag = ""
        if any(k in name for k in ("stereo mix", "what u hear", "cable", "vb-audio", "virtual", "waveout", "point")):
            tag = "   <-- system/loopback capture"
        print(f"  [{i}] {d['name']}{tag}")

    if default_mode is None:
        print("\nSelect input source:")
        print("  1) Microphone")
        print("  2) System / Speaker audio (Stereo Mix, What U Hear, or VB-Audio Cable)")
        while True:
            choice = input("> Choice (1/2): ").strip()
            if choice in ("1", "2"):
                break
        mode = "system" if choice == "2" else "mic"
    else:
        mode = default_mode

    if mode == "system":
        default_idx = _first_loopback_device()
        hint = f" [default {default_idx}]" if default_idx is not None else " (no loopback device auto-detected — pick a Stereo Mix / VB-Audio Cable input)"
    else:
        default_idx = _default_device("in")
        hint = f" [default {default_idx}]"

    sel = input(f"> Device index to capture from{hint}: ").strip()
    cfg.device_index = int(sel) if sel else default_idx

    name = devs[cfg.device_index]["name"].lower() if 0 <= cfg.device_index < len(devs) else ""
    if mode == "system" and any(k in name for k in ("microphone", "mic", "headset", "line in")):
        print(
            "⚠️  WARNING: device looks like a MICROPHONE, not a system loopback. "
            "For system audio, pick a 'Stereo Mix' / 'VB-Audio Cable' input AND route "
            "your playback into that cable.\n"
        )

    if mode == "system":
        print(
            "Reminder: route your meeting/system audio into that device — enable "
            "'Stereo Mix' (Recording devices > Show disabled > Enable) or set your "
            "playback device to a VB-Audio Virtual Cable.\n"
        )
    cfg.echo_transcript = mode == "mic"
    print(f"-> Capturing device {cfg.device_index}\n")
    return cfg


def build_config() -> Config:
    cfg = load_config()
    p = argparse.ArgumentParser(description="Real-time interview assistant")
    p.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    p.add_argument("--device", type=int, default=None, help="Input/loopback device index")
    p.add_argument("--test-capture", action="store_true",
                   help="Record 5s, print RMS/noise-floor/VAD stats, then exit (no STT/LLM/Telegram)")
    p.add_argument("--loopback", action="store_true", help="Capture system/internal audio instead of mic")
    p.add_argument("--mic", action="store_true", help="Capture microphone (skip interactive prompt)")
    p.add_argument("--silence-ms", type=int, default=None)
    p.add_argument("--stt-model", type=str, default=None)
    p.add_argument("--llm-model", type=str, default=None)
    p.add_argument("--chat-id", type=str, default=None)
    p.add_argument("--cv", type=str, default=None, help="Path to CV file (.txt/.pdf/.docx)")
    args = p.parse_args()

    if args.list_devices:
        AudioCapture.list_devices()
        raise SystemExit(0)

    if args.device is not None:
        cfg.device_index = args.device
    if args.silence_ms is not None:
        cfg.silence_ms = args.silence_ms
    if args.stt_model:
        cfg.stt_model = args.stt_model
    if args.llm_model:
        cfg.llm_model = args.llm_model
    if args.chat_id:
        cfg.telegram_chat_id = args.chat_id
    if args.cv:
        cfg.cv_path = args.cv
    cfg._test_capture = args.test_capture
    cfg._loopback = args.loopback

    if cfg.device_index is None:
        default_mode = "system" if args.loopback else ("mic" if args.mic else None)
        choose_input_source(cfg, default_mode=default_mode)
    else:
        cfg.echo_transcript = not args.loopback

    prompt_file = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
    if os.path.exists(prompt_file):
        cfg.system_prompt = open(prompt_file, encoding="utf-8").read().strip()

    if not cfg.cv_path:
        cv = input("CV file path (Enter to skip): ").strip()
        if cv:
            cfg.cv_path = cv

    return cfg


def main():
    cfg = build_config()

    if getattr(cfg, "_test_capture", False):
        if cfg.device_index is None:
            choose_input_source(cfg, default_mode="system" if getattr(cfg, "_loopback", False) else None)
        asyncio.run(AudioCapture.test_capture(cfg))
        return

    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        print(
            "⚠️  ACTION NEEDED: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (in .env) "
            "or replies won't reach Telegram. Transcripts will still print locally."
        )
    if not cfg.llm_api_key:
        print(
            "⚠️  ACTION NEEDED: set OPENROUTER_API_KEY (in .env) or the LLM step will fail."
        )

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    orchestrator = Orchestrator(cfg)
    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
