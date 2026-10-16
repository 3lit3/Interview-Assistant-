import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen


# ── Console must survive non-UTF-8 code pages ────────────────────────────────
# Every status line in this file carries an emoji, and on a legacy Windows
# console the active code page is the system one — cp1254 in Turkey, cp1251 in
# Russia, cp932 in Japan. Printing an emoji there raises UnicodeEncodeError, and
# because most of these prints sit inside the receive loop it takes the session
# down on startup. Reconfiguring to UTF-8 with a replacement fallback costs
# nothing and makes the app launch the same way in every locale.
import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw / redirected pipes / anything exotic — never fatal

# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import os
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier,
)

# The file-backed tools (open_app, web_search, browser_control, …) are no longer
# imported or declared here — they self-describe via a TOOL dict in their own
# actions/*.py file and are auto-discovered by core.action_loader at startup.
# Only tools that are tied to live-session state stay inline in this file
# (screen_process, close_camera, save_memory, manage_monitor, shutdown_jarvis,
# system_status).
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import (
    get_brief_enabled, get_media_resolution, get_proactive_audio_enabled,
    get_push_to_talk_enabled, get_thinking_enabled, get_turn_tuning, get_voice,
    get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
)
from core                     import gemini as _gemini
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.action_loader        import discover_actions
from core.echo                 import EchoGuard
from core.viseme               import VisemeStream
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

# How long the assistant stays awake with no user speech before it auto-sleeps
# again (wake-word mode only).
WAKE_SLEEP_TIMEOUT = 120.0   # seconds (2 minutes)

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
# The conversation's model. A NAME, not a decision: the ladder lives in
# core/gemini.py and this is only whichever rung is currently in use, kept here
# as a module attribute because plugins read it (chat_takeover asks main for it
# so that upgrading the assistant upgrades the plugin too).
#
# It is reassigned on every connect, so a model that runs out of quota is
# stepped over and the assistant keeps talking instead of failing to start.
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


# ── Viseme extraction ─────────────────────────────────────────────────────────
# The avatar's mouth used to be driven by one RMS value per ~200 ms write batch,
# which is five updates a second averaged over a fifth of a second — it could
# only ever flap. These read the *shape* of each 20 ms slice straight from the
# spectrum of the audio being played, so no transcript, no forced alignment and
# no language assumption: it works the same for Turkish and English.
#
# Two numbers come out. Openness tracks the first formant — F1 climbs as the jaw
# drops, so /a/ reads open and /i/ or /u/ read closed. Width tracks the second —
# F2 is high for spread vowels (/i/, /e/) and low for rounded ones (/u/, /o/).
# Extra time beyond the device's reported output latency before the microphone
# is trusted again: covers room decay and the speaker's own settling.
_TAIL_MARGIN = 0.25

_VIS_WIN = 1024        # ~43 ms analysis window at 24 kHz: enough for formants
_VIS_HOP = 480         # 20 ms between frames, i.e. 50 shapes a second

# Delay from handing the first bytes of a reply to an already-running output
# stream to hearing them: one callback period, plus whatever the DAC adds.
_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE      # ~43 ms
# How far past the device's own buffer the mouth's timeline may drift before it
# is re-anchored. The buffer is the hard limit on how much audio can be queued
# ahead, so anything beyond it plus a margin for clock error is impossible.
_CURSOR_SLACK = 0.15

# Erring early is the safe direction. A viewer tolerates a mouth that moves
# slightly before the sound far better than one that moves after it — the
# broadcast limits are about 45 ms of lag against 125 ms of lead — so where
# this is uncertain it is biased to lead.


def _pcm_visemes(samples, sr: int = 24000):
    """Slice a PCM block into (level, openness, width) frames, one per 20 ms.

    Returns [] on anything unexpected — the mouth falls back to loudness-only
    articulation rather than the caller having to handle an error.
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size < _VIS_WIN:
            return []
        win = np.hanning(_VIS_WIN).astype(np.float32)
        freqs = np.fft.rfftfreq(_VIS_WIN, 1.0 / sr)
        b_f1_lo = (freqs >= 150) & (freqs < 450)     # F1 of close vowels
        b_f1_hi = (freqs >= 450) & (freqs < 1100)    # F1 of open vowels
        b_f2_bk = (freqs >= 600) & (freqs < 1300)    # F2 of rounded vowels
        b_f2_fr = (freqs >= 1700) & (freqs < 3200)   # F2 of spread vowels
        b_hiss = (freqs >= 3800) & (freqs < 8000)    # fricatives

        # One frame per hop across the *whole* block. Stepping only while a full
        # window fits stopped 1024 - 480 samples short of the end, so a 200 ms
        # batch yielded 160 ms of schedule: the mouth ran out of frames before
        # the audio ran out of sound, and each batch no longer lined up with the
        # end of the one before it. Losing 20 % of every batch is most of why
        # the mouth did not track the words.
        out = []
        for start in range(0, x.size, _VIS_HOP):
            # The level gates closures, so it is measured over exactly this
            # 20 ms and never looks ahead. The spectrum needs a longer window
            # to resolve formants and may be short-filled at the very end.
            level = _pcm_level(x[start:start + _VIS_HOP])
            seg = x[start:start + _VIS_WIN]
            if seg.size < _VIS_WIN:
                seg = np.concatenate([seg, np.zeros(_VIS_WIN - seg.size,
                                                    dtype=np.float32)])
            if level <= 0.0:
                out.append((0.0, 0.0, 0.0))
                continue
            mag = np.abs(np.fft.rfft((seg - seg.mean()) * win))
            f1l, f1h = float(mag[b_f1_lo].sum()), float(mag[b_f1_hi].sum())
            f2b, f2f = float(mag[b_f2_bk].sum()), float(mag[b_f2_fr].sum())
            hiss = float(mag[b_hiss].sum())

            openness = f1h / (f1l + f1h + 1e-6)
            width = (f2f - f2b) / (f2f + f2b + 1e-6)
            # A wide-open jaw physically cannot purse, so openness damps width.
            # /a/ has a low enough F2 to read as "rounded" on the bands alone;
            # letting openness suppress the width term is what keeps an open
            # vowel from pursing.
            width *= (1.0 - openness) ** 0.8
            # Fricatives are formed with a nearly closed mouth.
            h = hiss / (f1l + f1h + f2b + f2f + hiss + 1e-6)
            openness *= 1.0 - 0.65 * min(1.0, h * 2.5)
            out.append((level,
                        float(min(1.0, max(0.0, openness))),
                        float(min(1.0, max(-1.0, width)))))
        return out
    except Exception:
        return []


def _describe_tools(declarations) -> str:
    """One line per capability, straight from the live tool declarations.

    Derived rather than written down: the action and plugin registries are
    discovered at startup, so whatever the user has installed is what the model
    is told it can do. Adding a plugin extends this by itself, and removing one
    stops the model from claiming an ability it no longer has.
    """
    lines = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict)
                    else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:150]}" if desc else f"- {name}")
    return "\n".join(lines)


def _describe_limits(has_vision: bool, has_mic: bool) -> str:
    """The other half of self-knowledge: what is out of reach, and why.

    Derived from how the program is actually built, not from a list of refusals.
    A model that knows its boundaries stops improvising around them, and stating
    them as architecture rather than as rules keeps the answer honest in any
    language.
    """
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\n".join(out)


def _render_prompt(template: str, values: dict) -> str:
    """Fill {tokens} in the prompt template.

    A plain replace rather than str.format: the file is meant to be edited by
    hand, and a stray brace in someone's own wording must never take the app
    down at startup.
    """
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are a real-time interview answer assistant. "
            "Generate the exact answer the candidate should give. "
            "Output only the answer, in first person. Never mention being an AI."
        )


def _patch_api_cfg(**fields) -> None:
    """Read-modify-write api_keys.json, never raising."""
    try:
        data = {}
        if API_CONFIG_PATH.exists():
            data = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        data.update(fields)
        API_CONFIG_PATH.write_text(json.dumps(data, indent=4), encoding="utf-8")
    except Exception:
        pass


def _load_cv_text(path: str) -> str:
    """Extract text from a CV file (.pdf/.docx/.doc/.txt/.md).

    Raises ValueError with a user-readable reason whenever the file cannot be
    turned into text — an empty return must mean "this file really has no
    words in it", never "something silently broke". The original version
    swallowed every failure: it imported pypdf (which is NOT installed —
    requirements.txt ships pdfplumber/PyPDF2 instead), caught the ImportError
    in a bare except and returned "", so every PDF reported as
    "Could not read that CV." with no hint why.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _load_pdf_text(path)
    if ext == ".doc":
        # The binary .doc format needs Word itself; python-docx reads only
        # the XML-based .docx and throws on the old format.
        raise ValueError(
            "legacy .doc can't be parsed — open it in Word and "
            "'Save As' .docx (or export a PDF), then upload again")
    if ext == ".docx":
        try:
            import docx
            document = docx.Document(path)
        except Exception as e:
            raise ValueError(f"Word document unreadable — {e}")
        parts = [p.text for p in document.paragraphs]
        # CVs often lay skills and dates out in tables — paragraph iteration
        # skips those cells entirely.
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        parts.append(cell.text)
        text = "\n".join(parts)
        if not text.strip():
            raise ValueError("document contains no text")
        return text
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except Exception as e:
        raise ValueError(f"file unreadable — {e}")
    if not text.strip():
        raise ValueError("file is empty")
    return text


def _load_pdf_text(path: str) -> str:
    """PDF → text, trying every reader this project actually ships.

    pdfplumber and PyPDF2 are declared in requirements.txt (the file
    processor already uses them); pypdf is accepted as a third option for
    environments that happen to carry it. The first reader that yields real
    text wins; the LAST attempt's failure reason is what gets reported,
    because "no selectable text" (a scanned/image-only PDF) is the common
    real cause.
    """
    def _plumber(p):
        import pdfplumber
        with pdfplumber.open(p) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)

    def _pypdf2(p):
        from PyPDF2 import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(p).pages)

    def _pypdf(p):
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(p).pages)

    last = "no PDF reader available"
    for opener in (_plumber, _pypdf2, _pypdf):
        try:
            text = opener(path)
        except Exception as e:
            last = str(e) or type(e).__name__
            continue
        if text.strip():
            return text
        last = "no selectable text (is it a scanned/image-only PDF?)"
    raise ValueError(f"PDF text extraction failed — {last}")

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# Transcript chunks shorter than this may legitimately repeat ("evet, evet"),
# so only longer ones are treated as duplicates.
_REPEAT_MIN = 12

# Telegram Bot API allows roughly one write per second per chat; exceeding it
# returns 429 and repeated flooding can get the bot temporarily banned. The
# live-mirror worker waits out this gap before every send/edit/delete.
_TG_WRITE_GAP = 1.1


def _tg_coalesce(batch: list) -> list:
    """Collapse a drained batch of live-mirror ops before executing them.

    The transcript pushes a chunk every ~70 ms while Telegram writes at most
    once per second, so the queue piles up. Processing pushes one by one would
    make the message lag further behind with every item; instead each run of
    consecutive pushes collapses to its LAST text — which is exactly what the
    next scheduled write should show anyway.

    A run is dropped entirely when an end/delete follows it: that operation
    supersedes whatever the pushes were building toward (end carries the final
    words itself, delete retracts the message), so executing the pushes first
    would only create a message that is immediately edited or destroyed.
    """
    out, i = [], 0
    while i < len(batch):
        if batch[i][0] != "push":
            out.append(batch[i])
            i += 1
            continue
        last = None
        while i < len(batch) and batch[i][0] == "push":
            last = batch[i][1]
            i += 1
        if i < len(batch) and batch[i][0] in ("end", "delete"):
            continue                      # superseded by what follows
        if last:
            out.append(("push", last))
    return out


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    """True if this transcript chunk has already been seen this turn.

    Guards against the API re-sending the tail of a response across the several
    turn_completes a tool-using turn produces.
    """
    if len(txt) < _REPEAT_MIN:
        return bool(buf) and txt == buf[-1]
    joined = " ".join(buf)
    return txt in joined

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    # ── Inline tools ─────────────────────────────────────────────────────────
    # These stay here (rather than in an actions/*.py TOOL dict) because their
    # handling is woven into live-session state — vision capture/injection,
    # camera stream, memory writes, the monitor engine, and shutdown. All other
    # tools live in their own action file and are auto-discovered by
    # core.action_loader (see JarvisLive.__init__).
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when the user says (in ANY language): close camera, stop camera, "
            "turn off camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "JARVIS checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop it. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


def _error_text(exc: BaseException) -> str:
    """The message that actually explains a failure.

    A task dying inside the session TaskGroup arrives as a BaseExceptionGroup
    whose str() is only "unhandled errors in a TaskGroup (1 sub-exception)" —
    the real reason (APIError 1011, 429, a socket error) lives in the leaves.
    Every classification downstream used str(e), so it read that one line and
    matched nothing: no quota step-down, no API-key prompt, no network backoff,
    and one catastrophic false positive (see _is_handle_rejection).
    """
    if isinstance(exc, BaseExceptionGroup):
        return " | ".join(_error_text(sub) for sub in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def _is_handle_rejection(text: str) -> bool:
    """True only when the server itself refused the session-resumption handle.

    The old test was `"handle" in str(e).lower()` — and "un**handle**d errors in
    a TaskGroup" contains it, so EVERY crash of a resumed session was reported
    as "Resumption handle rejected", threw away a perfectly good handle (the
    conversation was lost) and reconnected with zero backoff. The word now has
    to appear next to "resum", which only a genuine rejection message contains.
    """
    low = text.lower()
    return "resum" in low and any(
        k in low for k in ("handle", "invalid", "not found", "expired", "unknown")
    )


def _seconds_left(value) -> float | None:
    """GoAway.time_left is a protobuf Duration string such as "540s"."""
    if value is None:
        return None
    try:
        return float(str(value).strip().rstrip("sS"))
    except (TypeError, ValueError):
        return None


class JarvisLive:
    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._asst_name     = "JARVI    S"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        # Transcript-driven mouth shapes for the avatar. Fed from the receive
        # loop as words arrive, drained by the playback loop against the audio.
        self._visemes              = VisemeStream()
        self._last_out_logged      = ""      # de-dupes a re-sent transcript tail
        # Push-to-talk
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None    # core.hotkey.PushToTalk
        self._out_level            = 0.0     # level of the audio being played right now
        self._echo                 = EchoGuard()
        # `stream.write()` returns when the buffer accepts the audio, not when the
        # speaker has finished with it, so sound is still in the room after the
        # speaking flag drops. Streaming the microphone during that gap is how an
        # assistant ends up answering itself. Measured from the device rather than
        # guessed; see _play_audio.
        self._out_latency          = 0.20    # seconds, replaced with the real value
        self._tail_until           = 0.0     # monotonic time the echo tail expires
        # Wall-clock time at which the audio written next will begin to sound.
        # The mouth is scheduled against this, never against "now": batches are
        # handed to the device far faster than they play, so "now" ran the lips
        # ahead of the words and cut every schedule short. 0 = nothing playing.
        self._play_cursor          = 0.0
        self.ui.on_push_to_talk   = self.set_push_to_talk
        self.ui.ptt_hold          = self._on_ptt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self.ui.on_cv_loaded          = self._on_cv_loaded
        self.ui.on_interview_context  = self._on_interview_context
        self.ui.on_telegram_config    = self._on_telegram_config
        self.ui.on_telegram_link      = self._on_telegram_link
        # Interview mode: answers are shown in the suggestions panel and never
        # played aloud — a spoken answer would be heard by the interviewer, and
        # with SYSTEM SOUND on it would feed straight back into the loopback.
        try:
            _icfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
        except Exception:
            _icfg = {}
        self._interview_mode = bool(_icfg.get("interview_mode", True))
        self._speak_answers  = bool(_icfg.get("speak_answers", False))
        self._tg_token  = str(_icfg.get("telegram_bot_token", "") or "")
        self._tg_chat   = str(_icfg.get("telegram_chat_id", "") or "")
        self._tg_send_t = 0.0   # last successful telegram send (dedupe)
        # Relay mode (Mode B): no own bot token → every write goes through
        # the license server's shared bot, keyed by the license itself. The
        # boot fetch resolves which chat paid for it (and wakes Render
        # before the first message is due).
        self._tg_key = str(_LICENSE[0]) if _LICENSE else ""
        if not self._tg_token and self._tg_key:
            threading.Thread(target=self._tg_auto_chat, daemon=True,
                             name="tg-whoami").start()
        # Live-mirror queue: the receive loop enqueues, one dedicated worker
        # thread owns every Telegram write (and the message_id of the answer
        # it is editing), so there is no lock and writes arrive in order.
        self._tg_q: "queue.Queue | None" = None
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        # Set when the server has sent its go_away notice for the current
        # socket, so the warning is acted on exactly once per session.
        self._goaway_pending = False
        self._goaway_task: asyncio.Task | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it
        self._tuned_live    = True  # turn-taking / media / thinking knobs; same fallback

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            # Console gets the full boot transcript; the activity log gets only
            # what the user has to know about. Every plugin loading correctly is
            # the expected case and does not belong in their conversation.
            logger=lambda msg: print(f"[Plugins] {msg}"),
            notify=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ settings tab
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel

        # ── Wake word ────────────────────────────────────────────────────────
        # _awake gates the mic (see _listen_audio) and the background speakers.
        # It is True whenever wake word is OFF, so default behaviour is unchanged.
        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT

        # Restore the saved push-to-talk preference. Doing it here rather than
        # in __init__ means the hotkey thread only exists once there is a
        # session to talk to.
        if get_push_to_talk_enabled():
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] ⚠ Push-to-talk unavailable: {e}")
        # UI control surface for the Wake Word settings section.
        self.ui.wake_is_ready    = wake_is_ready          # () -> bool
        self.ui.wake_get_state   = self._wake_state       # () -> dict
        self.ui.on_wake_toggle   = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_wake_manual   = self._ui_wake_manual   # () -> toggle awake/asleep
        self.ui.on_wake_install  = self._ui_wake_install  # () -> (ok, msg)

    # ── Wake word: state machine ─────────────────────────────────────────────

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: print(f"[Wake] {m}"),
                notify=lambda m: self.ui.write_log(f"SYS: {m}"),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")

    # ── Wake word: UI callbacks (called from the Qt thread) ──────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        # Triggered by the user pressing the button, so its progress is exactly
        # what they are waiting to see.
        return wake_install(logger=lambda m: print(f"[Wake] {m}"),
                            notify=lambda m: self.ui.write_log(f"SYS: {m}"))

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    async def _on_go_away(self, time_left) -> None:
        """Rebuild the session on the server's own schedule.

        Called once per socket when go_away arrives. The resumption handle is
        replayed, so nothing in the conversation is lost — the point is to move
        BEFORE the deadline instead of being cut off by it. The wait is for the
        session to go quiet (answer finished, speaker drained) so an answer in
        flight is never sliced in half; the deadline itself is the backstop for
        a conversation that never pauses.
        """
        secs  = _seconds_left(time_left) or 600.0
        slack = 20.0 if secs > 30.0 else max(secs * 0.2, 1.0)
        deadline = time.monotonic() + max(secs - slack, 0.0)
        quiet = 0
        print(f"[JARVIS] 🔗 Server window closes in {secs:.0f}s — "
              "will hand the conversation over first.")
        while time.monotonic() < deadline:
            q = self.audio_in_queue
            idle = (not self._is_speaking
                    and (q is None or q.empty())
                    and not (self._turn_done_event and self._turn_done_event.is_set()))
            # Idle must hold for ~2 s: the gap between the user finishing and
            # the model's first audio is usually under that, and reconnecting
            # inside it would drop the answer being generated.
            quiet = quiet + 1 if idle else 0
            if quiet >= 4:
                break
            await asyncio.sleep(0.5)
        if self._reconnect_event is None:
            return
        print("[JARVIS] 🔗 Handing the conversation to a fresh session.")
        self.request_reconnect(keep_context=True, reason="session resumption")

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        # Respect wake-word sleep: a typed command must not be answered while
        # asleep either (the sleep gate is not just for the mic). Wake first with
        # "Hey Jarvis" or the WAKE NOW button.
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW first.")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    # ── Interview wiring: CV, context, telegram ──────────────────────────────

    def _on_cv_loaded(self, path: str):
        """A CV was dropped/picked. Persist it and fold it into this session."""
        if not self._interview_mode:
            return
        name = os.path.basename(path)
        try:
            _patch_api_cfg(cv_path=path)
        except Exception:
            pass
        try:
            text = _load_cv_text(path)
        except ValueError as e:
            self.ui.write_log(f"ERR: Could not read that CV — {e}")
            self.ui.set_cv_status(False, name)
            return
        except Exception as e:
            self.ui.write_log(
                f"ERR: Could not read that CV — {type(e).__name__}: {e}")
            self.ui.set_cv_status(False, name)
            return
        if not text:
            self.ui.write_log("ERR: Could not read that CV — no text found.")
            self.ui.set_cv_status(False, name)
            return
        self.ui.set_cv_status(True, name)
        self.ui.write_log(f"SYS: CV context updated — {len(text):,} chars")
        self._send_context(
            "[THE CANDIDATE'S CV]\n"
            "This is the candidate's real background. Ground every answer in it "
            "and never invent experience not listed here:\n\n" + text.strip()[:8000]
        )

    def _on_interview_context(self, text: str):
        """Context typed in the input box — role/company/topic for this call."""
        if not text.strip():
            return
        try:
            _patch_api_cfg(interview_context=text.strip())
        except Exception:
            pass
        self._send_context(
            "[INTERVIEW CONTEXT]\n"
            "Additional context for this interview — use it to tailor answers:\n\n"
            + text.strip()[:3000]
        )
        self.ui.write_log("SYS: Interview context applied.")

    def _send_context(self, body: str):
        """Fold text into the live session WITHOUT asking for a reply.

        turn_complete=False is the whole point: the model must absorb the CV or
        the context silently instead of answering it out loud.
        """
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": body}]},
                turn_complete=False
            ),
            self._loop
        )

    def _on_telegram_config(self, token: str, chat_id: str):
        self._tg_token = (token or "").strip()
        self._tg_chat  = (chat_id or "").strip()

    def _on_telegram_link(self, on_status=None) -> str:
        """Overlay button: one-tap link of the payment chat (Mode B).
        Blocking by design — the overlay runs it on its own thread."""
        if not self._tg_key:
            return ""
        import licensing
        return licensing.link_chat_flow(self._tg_key, on_status=on_status)

    def _tg_auto_chat(self) -> None:
        """Boot: resolve the linked payment chat for relay mode and persist it."""
        chat = ""
        try:
            from licensing import whoami
            chat = str(whoami(self._tg_key).get("chat_id") or "")
        except Exception as exc:
            print(f"[tg] whoami failed: {exc}", flush=True)
            return
        if not chat:
            print("[tg] license has no linked chat yet — use the Telegram "
                  "settings “Link chat from payment” button", flush=True)
            return
        self._tg_chat = chat
        try:
            from ui import _patch_config_keys
            _patch_config_keys(telegram_chat_id=chat)
        except Exception:
            pass
        print(f"[tg] relay mode — delivery chat {chat}", flush=True)

    def _tg_enqueue(self, kind: str, text: str = "") -> None:
        """Queue a Telegram write for the live-mirror worker.

        Single producer (the receive loop), so no lock: enqueue never blocks.
        "push" streams a growth of the current answer, "end" retires it with
        its finished text, "delete" retracts it (interviewer cut in),
        "status" sends an independent operational notice that never touches
        the live answer.
        """
        # getattr: this also runs from the system-sound supervisor, where a
        # missing attribute must degrade to "no Telegram" — an exception
        # thrown here would kill the capture task silently.
        # sendable = own token (direct) OR license key (server relay).
        if kind in ("push", "status") and (
                not (getattr(self, "_tg_token", "") or getattr(self, "_tg_key", ""))
                or not getattr(self, "_tg_chat", "")
                or not (text or "").strip()):
            return
        import queue as _q
        if getattr(self, "_tg_q", None) is None:
            self._tg_q = _q.Queue()
            threading.Thread(target=self._tg_worker, daemon=True,
                             name="tg-live").start()
        self._tg_q.put((kind, text))

    def _tg_api(self, method: str, payload: dict):
        """One Telegram write. Worker thread only. Returns the decoded JSON
        on success, None on any failure — Telegram being down, rate limited,
        or misconfigured must never disturb the audio path.

        Own bot token in the config → direct Bot API call (unchanged).
        No token, license key present → relay through the license server's
        shared bot (the token never ships in the app)."""
        if not getattr(self, "_tg_chat", ""):
            return None
        token = getattr(self, "_tg_token", "")
        if token:
            try:
                import requests
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/{method}",
                    json=payload, timeout=10,
                )
                data = r.json()
                if isinstance(data, dict) and data.get("ok"):
                    return data
            except Exception:
                pass
            return None
        key = getattr(self, "_tg_key", "")
        if not key:
            return None
        try:
            from licensing import tg_relay
            op = {"sendMessage": "push", "editMessageText": "edit",
                  "deleteMessage": "delete"}.get(method, "push")
            data = tg_relay(key, op, text=payload.get("text", ""),
                            message_id=payload.get("message_id"))
            if isinstance(data, dict) and data.get("ok"):
                return data
        except Exception:
            pass
        return None

    def _tg_worker(self) -> None:
        """Single writer for every Telegram message of the session.

        Because this one thread is the only consumer of the queue AND the
        only owner of the live message's id/text state, there is nothing to
        lock and writes stay ordered. Two behaviours live here:

        • Rate limiting — every API call waits out _TG_WRITE_GAP first, so a
          15 chunks/s transcript degrades to ~1 edit/s instead of a flood.
        • Coalescing — a drained batch of pushes collapses to its newest
          text, keeping the phone screen tight behind the panel instead of
          replaying stale fragments one per second.
        """
        import queue as _q
        live_id = None        # message_id of the answer being streamed
        live_text = ""        # what that message currently shows
        last_api = 0.0        # monotonic time of the last successful write

        def wait_turn():
            nonlocal last_api
            gap = time.monotonic() - last_api
            if gap < _TG_WRITE_GAP:
                time.sleep(_TG_WRITE_GAP - gap)
            last_api = time.monotonic()

        while True:
            batch = [self._tg_q.get()]
            while True:                      # drain everything queued right now
                try:
                    batch.append(self._tg_q.get_nowait())
                except _q.Empty:
                    break
            for kind, text in _tg_coalesce(batch):
                text = (text or "").strip()[:4000]
                if kind == "push":
                    if not text:
                        continue
                    if live_id is None:
                        wait_turn()
                        resp = self._tg_api(
                            "sendMessage",
                            {"chat_id": self._tg_chat, "text": text})
                        if resp and resp.get("result"):
                            try:
                                live_id   = int(resp["result"]["message_id"])
                                live_text = text
                            except Exception:
                                live_id = None
                    elif text != live_text:
                        wait_turn()
                        if self._tg_api(
                                "editMessageText",
                                {"chat_id": self._tg_chat,
                                 "message_id": live_id, "text": text}):
                            live_text = text
                elif kind == "end":
                    if live_id is not None:
                        if text and text != live_text:
                            wait_turn()
                            if self._tg_api(
                                    "editMessageText",
                                    {"chat_id": self._tg_chat,
                                     "message_id": live_id, "text": text}):
                                live_text = text
                        live_id, live_text = None, ""
                    elif text:
                        # Nothing streamed to Telegram this turn (configured
                        # mid-answer, or every push was coalesced away by a
                        # fast end) — mirror the finished answer as-is.
                        wait_turn()
                        self._tg_api(
                            "sendMessage",
                            {"chat_id": self._tg_chat, "text": text})
                elif kind == "delete":
                    if live_id is not None:
                        wait_turn()
                        self._tg_api(
                            "deleteMessage",
                            {"chat_id": self._tg_chat,
                             "message_id": live_id})
                        live_id, live_text = None, ""
                elif kind == "status":
                    # Operational notices (capture failure/recovery): plain
                    # independent messages that never touch the live answer
                    # being streamed above.
                    wait_turn()
                    self._tg_api(
                        "sendMessage",
                        {"chat_id": self._tg_chat, "text": text})

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
        else:
            # Hold the guard open across the device's own output latency plus a
            # margin for the room. The microphone is NOT muted during it — the
            # guard still lets a genuine reply through, so answering instantly
            # still works. Only our own echo is dropped.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            # The echo history is deliberately NOT cleared here: the tail above
            # still needs it to recognise our own voice. It is dropped when the
            # tail expires. What the guard learned about the room always stays.
            self._out_level = 0.0
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        # A window-scoped chord is a real limitation, not a detail — say it once
        # in the log so nobody wonders why it does nothing while another app is
        # focused. Reporting it must never be able to undo the thing it reports.
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def _on_ptt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        if held:
            # Holding the key is also a way to wake it, so push-to-talk works
            # without having to say the wake word first.
            if self._wake_enabled and not self._awake:
                self._awake = True
                self._last_user_speech = time.monotonic()
        try:
            self.ui.set_state("LISTENING" if held else "SLEEPING")
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        # The words we were about to mouth are never going to be spoken now.
        self._visemes.reset()
        self._play_cursor = 0.0     # next batch starts a fresh timeline
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        _cfg = {}
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Interview mode: the identity block is replaced with the interview
        # assistant persona and personal-assistant memory is NOT injected —
        # otherwise "[IDENTITY] You are JARVIS..." overrides prompt.txt and the
        # model reverts to being a general assistant.
        if _cfg.get("interview_mode", True):
            identity_ctx = (
                "[IDENTITY]\n"
                "You are a real-time interview answer assistant. You are NOT a "
                "general personal assistant. Your job is to listen to the "
                "interview through the microphone and generate the exact answer "
                "the candidate should give. Never fall back to being a personal "
                "assistant, never "
                "offer help with general tasks, never mention being an AI.\n\n"
            )
            mem_str = ""  # personal-assistant memory does not belong in an interview
        else:
            _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                     if _user_name
                     else 'ADDRESS: Address the user with the ordinary respectful form '
                          'for a superior in the language you are currently speaking — '
                          '"sir" in English, its everyday equivalent in any other '
                          'language. Never an archaic or aristocratic form, and never '
                          'the form from a different language than the one you are '
                          'speaking in this sentence.')
            identity_ctx = (
                f"[IDENTITY]\n"
                f"Your name is {self._asst_name}. "
                f"Always refer to yourself as {self._asst_name}.\n"
                f"{_addr}\n\n"
            )

        # Everything the model is told about *itself* is derived here, not
        # written into prompt.txt: the name comes from config, the platform from
        # the host, the capability list from the registries that were just
        # discovered. Rename the assistant, add a plugin or move to another OS
        # and this follows without anyone editing a prompt.
        _all_decls = (TOOL_DECLARATIONS
                      + self._action_registry.get_tool_declarations()
                      + self._plugin_registry.get_tool_declarations())
        _names = {(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        if _cfg.get("interview_mode", True):
            # Only screen reading is useful during an interview (shared
            # questions / code on screen). Everything else — browser, files,
            # settings, reminders — is a distraction that pulls the model back
            # into personal-assistant mode.
            _allowed = {"screen_process"}
            _all_decls = [
                d for d in _all_decls
                if (d.get("name") if isinstance(d, dict) else getattr(d, "name", "")) in _allowed
            ]
            _names = _allowed
        sys_prompt = _render_prompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "capabilities": _describe_tools(_all_decls),
            "limits": _describe_limits(
                has_vision="screen_process" in _names,
                has_mic=True,
            ),
        })

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        # CV context injection — candidate's real background for interview answers
        try:
            _cv_path = (_cfg.get("cv_path") or "").strip()
            if _cv_path and os.path.isfile(_cv_path):
                try:
                    _cv_text = _load_cv_text(_cv_path)
                except Exception as _e:
                    # Not silent: a CV that can't be parsed must be visible,
                    # or the model answers interviews knowing nothing about
                    # the candidate with no clue in the log as to why.
                    _cv_text = ""
                    self.ui.write_log(
                        f"ERR: Saved CV '{os.path.basename(_cv_path)}' not "
                        f"loaded — {_e}. Re-upload it.")
                if _cv_text:
                    parts.append(
                        "[THE CANDIDATE'S CV]\n"
                        "Use this background when generating interview answers. "
                        "Never invent experience not listed here:\n\n"
                        + _cv_text.strip()[:8000]
                    )
        except Exception:
            pass

        # Interview context typed into the input box last session — restored so
        # a reconnect does not silently forget the role/company/topic.
        try:
            _ctx = (_cfg.get("interview_context") or "").strip()
            if _ctx:
                parts.append(
                    "[INTERVIEW CONTEXT]\n"
                    "Additional context for this interview — tailor answers to it:\n\n"
                    + _ctx[:3000]
                )
        except Exception:
            pass

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": _all_decls}],
            # Asks the server to issue session-resumption handles. Enabled but
            # NOT yet used: no handle is captured from the resumption update and
            # none is passed back here, so a reconnect starts a fresh session
            # rather than continuing the old one. Left on because the handles
            # cost nothing and are the prerequisite for doing it properly.
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — JARVIS can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: JARVIS stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            if get_proactive_audio_enabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self._tuning_config())

        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        # How long the server waits through a pause before deciding your turn is
        # over. This — not the size of the prompt — is what most of the delay
        # before a reply actually is, and the default has to suit everybody, so
        # it is necessarily cautious.
        turn = get_turn_tuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        # Screenshots and camera frames are tokenised at this resolution and then
        # stay in the session's context. 'medium' keeps on-screen text legible
        # for a fraction of a full-resolution frame.
        res = get_media_resolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        # Thinking is left at the server default deliberately. Forcing the budget
        # to zero was measured on gemini-3.1-flash-live over interleaved trials
        # and did not make the first word arrive sooner — this model does not
        # appear to deliberate on the Live path, so pinning the field only adds a
        # way for a future release to behave differently. Set "thinking_enabled"
        # in config/api_keys.json to true to let it reason instead.
        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")


        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    # The image is attached to this same exchange, so there is
                    # nothing to stall for and nothing to announce. Asking for an
                    # acknowledgement here is what produced two spoken answers —
                    # the model filled that turn by answering the question from
                    # imagination, then answered it again once it could see.
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured and attached to this "
                        f"same exchange. Do not acknowledge and do not answer yet — the image "
                        f"is arriving with this result. Reply once, from what you actually see "
                        f"in it."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")

        # A tool that declared itself NON_BLOCKING also says when its answer may
        # re-enter the conversation. Without this the model finishes whatever it
        # was saying and then reads the result out on top of it — which, for
        # something like a phone call already ringing, is exactly the noise the
        # non-blocking call was meant to avoid. Tools that declared nothing get
        # the API default and behave as they always have.
        _sched = (self._action_registry.scheduling(name)
                  or self._plugin_registry.scheduling(name))
        _extra = {"scheduling": _sched} if _sched else {}
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result},
            **_extra
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            # ── Wake-word gate ───────────────────────────────────────────────
            # While asleep, the mic audio NEVER goes to Gemini (nothing is
            # streamed, so JARVIS can't respond to speech not addressed to it and
            # nothing leaves the machine). Frames are instead handed to the local
            # detector, which runs its model in ITS OWN thread — the cost here is
            # only a queue push, so the audio path is never slowed. When wake word
            # is off (default) or we're awake, this is a single boolean check.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            # ── Barge-in ─────────────────────────────────────────────────────
            # While JARVIS talks the mic is not streamed, but it is still worth
            # listening to locally: if the user starts speaking, cut the answer
            # short the way a person would stop when interrupted.
            #
            # The whole difficulty is echo — on speakers the mic hears JARVIS.
            # So the test is not "is the mic loud" but "is the mic louder than
            # the echo of what we are playing right now", sustained long enough
            # that a cough or a keystroke cannot trigger it.
            if jarvis_speaking:
                # Nothing is streamed while JARVIS talks.
                #
                # Interrupting by voice used to live here: `EchoGuard` can pick a
                # user out from under our own echo, and `core/echo.py` still does
                # that for the tail below. Re-enabling is small — classify each
                # block here and call interrupt() after `required_blocks` of
                # agreement — but it depends on the listener's room, so it stays
                # out until it can be tried on real hardware.
                return

            # ── Echo tail ────────────────────────────────────────────────────
            # The speaking flag has dropped but the speakers have not finished.
            # Sending this to the model is how an assistant hears itself, decides
            # it was addressed, and answers its own last sentence. The microphone
            # stays OPEN — the guard only drops blocks that are our own voice, so
            # replying the instant it stops still works.
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        return
                    self._tail_until = 0.0      # a real voice ends the tail early
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            # ── Push-to-talk ─────────────────────────────────────────────────
            # When it is on the microphone is closed by default and the chord
            # opens it, which is the whole point: nothing leaves the machine
            # unless you are holding the key.
            if self._ptt_enabled and not self._ptt_held:
                return
            
            if not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                # The queue is bounded (200 frames ≈ a few seconds), so a send
                # that stalls fills it — and an unguarded put_nowait then raises
                # QueueFull inside the event-loop callback, once per mic frame,
                # flooding the log. Dropping the frame instead keeps the audio
                # path quiet; the model misses a few slices and carries on.
                def _enqueue(_d=data):
                    try:
                        self.out_queue.put_nowait(
                            {"data": _d, "mime_type": "audio/pcm"})
                    except asyncio.QueueFull:
                        pass
                loop.call_soon_threadsafe(_enqueue)
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _listen_system_audio(self):
        """Supervise loopback capture, which runs in a CHILD PROCESS.

        WASAPI virtual endpoints (VB-Cable especially) invalidate their
        IAudioClient when the driver glitches; soundcard's next COM call then
        dies with an access violation no try/except can catch. In-process that
        killed the whole app — faulthandler put the stack exactly in
        soundcard's GetDevicePeriod, its silence-idle path, on the 16-channel
        cable endpoint. Out-of-process, the same death costs one silent second
        and a respawn: this task must never be the reason the app goes down.
        """
        import queue as _q
        from collections import deque
        import multiprocessing as mp
        from core.sys_sound_capture import capture_loop

        # Session-independent by design: this task gates on ui.system_sound
        # and pushes into whichever out_queue the CURRENT session owns, so it
        # starts ONCE (from run()) and outlives reconnects. Living inside the
        # session TaskGroup was an incident: every reconnect spawned a fresh
        # supervisor and cancelled the old one mid-capture, and with no
        # cleanup path its child process was left running — stacked children
        # fought over the same 16-channel endpoint until none could open.
        self._sys_deaths = deque(maxlen=8)
        self._sys_was_broken = False

        while True:
            if not getattr(self.ui, "system_sound", False):
                await asyncio.sleep(0.2)
                continue
            ctx = mp.get_context("spawn")
            chunks = ctx.Queue()
            logs = ctx.Queue()
            stop = ctx.Event()
            # Phase-aware watchdog: from spawn until the child logs
            # "Listening" it is OPENING the device (COM enumeration), which
            # can legitimately take far longer than 8s under the CPU spike
            # that often accompanies a reconnect — the old clock ran from
            # spawn, killed healthy opening children, three "failures"
            # tripped the breaker, and capture stayed dead. Once listening,
            # blocks arrive ~15x/s: a 10s hole is a real wedge.
            listening = False
            _last_recv = time.time()
            _OPEN_GRACE = 20.0
            _STREAM_GAP = 10.0

            def drain_logs():
                # mp.Queue delivery goes through a feeder thread, so lines
                # can lag their put(); drain after death too, or a dying
                # child's last error message is lost.
                nonlocal listening
                while True:
                    try:
                        line = logs.get_nowait()
                    except _q.Empty:
                        break
                    print(f"[JARVIS] 🔈 {line}")
                    _low = line.lower()
                    if "listening" in _low:
                        listening = True
                    elif "reopening" in _low:
                        listening = False   # back to the open phase
                    try:
                        self.ui.write_log(line)
                        if "16 ch" in _low:
                            # VB-Cable's multi-channel endpoints crash under
                            # WASAPI loopback (access violations across
                            # ntdll/soundcard/Qt while that was the default).
                            # The stereo endpoint of the same cable is stable.
                            self.ui.write_log(
                                "WARN: 16-channel endpoint selected — loopback "
                                "here crashes easily. Set the STEREO "
                                "\"Speakers (VB-Audio Virtual Cable)\" as "
                                "Windows default instead.")
                    except Exception:
                        pass

            try:
                proc = ctx.Process(target=capture_loop, args=(chunks, logs, stop),
                                   name="sys-sound", daemon=True)
                proc.start()
            except Exception as e:
                # This task no longer lives in a TaskGroup, so an unhandled
                # exception here would kill it silently and capture would be
                # dead for the rest of the session. A spawn failure (rare:
                # handle exhaustion) retries instead of ending the task.
                msg = (f"ERR: Could not start system sound capture — {e}. "
                       f"Retrying in 5s.")
                print(f"[JARVIS] 🔈 {msg}")
                try:
                    self.ui.write_log(msg)
                except Exception:
                    pass
                await asyncio.sleep(5)
                continue
            self._sys_proc = proc
            print(f"[JARVIS] 🔈 System sound capture process started "
                  f"(pid {proc.pid})")
            # The child pushes a PCM block ~15x/s EVEN IN SILENCE (all
            # forwarding happens in the parent), so a silent stretch of
            # stream means capture is wedged — the 16-ch VB-Cable endpoint
            # hangs in CoCreateInstance instead of failing, leaving an
            # "alive" child that records nothing. Without this watchdog that
            # state is indistinguishable from health and system-sound
            # suggestions just stop, silently, until the user unmutes the
            # mic and the model hears the speakers instead.
            _last_recv = time.time()
            while getattr(self.ui, "system_sound", False) and proc.is_alive():
                drain_logs()
                try:
                    raw = await asyncio.to_thread(chunks.get, True, 0.4)
                    _last_recv = time.time()
                except _q.Empty:
                    timeout = _STREAM_GAP if listening else _OPEN_GRACE
                    if time.time() - _last_recv > timeout:
                        phase = "stream" if listening else "device open"
                        msg = (f"ERR: System sound capture wedged ({phase} — "
                               f"no audio for {int(timeout)}s). "
                               f"Restarting it...")
                        print(f"[JARVIS] 🔈 {msg}")
                        try:
                            self.ui.write_log(msg)
                        except Exception:
                            pass
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        break
                    continue
                except Exception:
                    break       # queue torn down — treat as child death
                if raw:
                    if self._sys_was_broken:
                        # The probe after the breaker actually delivered
                        # audio: the endpoint healed. Announce it — on the
                        # log and on the phone, because during a meeting the
                        # local log sits behind a shared screen.
                        self._sys_was_broken = False
                        note = "SYS: System sound recovered — capturing again."
                        print(f"[JARVIS] 🔈 {note}")
                        try:
                            self.ui.write_log(note)
                        except Exception:
                            pass
                        self._tg_enqueue(
                            "status",
                            "✅ System sound recovered — capturing again.")
                    self._push_sys_block(raw)
            stop.set()
            try:
                proc.join(3)
            except Exception:
                pass
            code = proc.exitcode
            if proc.is_alive():
                try:
                    proc.kill()
                except Exception:
                    pass
            drain_logs()
            crashed_recently = False
            if code not in (0, None):
                now = time.time()
                self._sys_deaths.append(now)
                # 70s, not 35: with the open-phase grace a single opening
                # attempt can cost ~20s, so three real failures can span
                # ~60s — a 35s window would watch them happen and never trip.
                crashed_recently = (len(self._sys_deaths) >= 3
                                    and now - self._sys_deaths[0] < 70)
                if crashed_recently:
                    msg = ("ERR: System sound capture keeps failing — "
                           "auto-probing every 60s until it recovers (toggle "
                           "SYSTEM SOUND off to stop). This happens when the "
                           "audio endpoint is unstable (the 16-channel "
                           "VB-Cable endpoint is). Set the STEREO "
                           "\"Speakers (VB-Audio Virtual Cable)\" as the "
                           "Windows default output.")
                else:
                    msg = (f"ERR: System sound capture died (exit code {code}) "
                           f"— respawning...")
            else:
                msg = "SYS: System sound capture stopped"
            print(f"[JARVIS] 🔈 {msg}")
            try:
                self.ui.write_log(msg)
            except Exception:
                pass
            if crashed_recently:
                # COOLDOWN + AUTO-PROBE, not the old dead-end wait for a
                # manual toggle. In the incident this replaced, the breaker
                # tripped on CPU-induced false kills, the endpoint healed
                # minutes later, and nothing ever retried — capture stayed
                # dead for the rest of the interview. Fast-cycling stays
                # forbidden; one probe per _SYS_RETRY_GAP cannot storm.
                self._sys_was_broken = True
                self._tg_enqueue(
                    "status",
                    "⚠️ System sound capture keeps failing — will keep "
                    "probing every 60s. If it persists, toggle SYSTEM SOUND "
                    "off and on.")
                waited = 0.0
                while (getattr(self.ui, "system_sound", False)
                       and waited < self._SYS_RETRY_GAP):
                    await asyncio.sleep(0.5)
                    waited += 0.5
                if not getattr(self.ui, "system_sound", False):
                    continue        # user turned it off during the cooldown
                self._sys_deaths.clear()
                probe = ("SYS: Probing system sound again — endpoint may "
                         "have recovered.")
                print(f"[JARVIS] 🔈 {probe}")
                try:
                    self.ui.write_log(probe)
                except Exception:
                    pass
                continue            # → spawn attempt
            await asyncio.sleep(0.5)

    # Seconds between breaker probes. Class attribute so tests can shrink it.
    _SYS_RETRY_GAP = 60.0

    def _push_sys_block(self, raw: bytes) -> None:
        """Forward one loopback block to the model — silence included.

        This used to be noise-gated: only blocks above a learned floor were
        sent, so the stream was speech-only bursts with hard gaps. The
        server's VAD needs continuous PCM — especially the trailing quiet
        after a speaker stops — to close an activity and generate. With the
        mic muted this stream was the ONLY input, the activity never
        closed, and no suggestion ever appeared; unmasking it as a
        "display bug" because unmuting the mic (whose path was never gated)
        let VAD fire at once and the model answered from the buffered
        question almost immediately. Forwarding every block makes system
        sound behave exactly like the microphone, which has always worked.
        """
        import numpy as np
        mono = np.frombuffer(raw, dtype=np.float32)
        if mono.size == 0:
            return
        try:
            rms = float(np.sqrt(np.mean(mono * mono)))
            self.ui.set_audio_level(min(rms, 1.0))
        except Exception:
            pass
        q = self.out_queue
        if q is None:
            return
        pcm = np.clip(mono, -1.0, 1.0)
        try:
            q.put_nowait({"data": (pcm * 32767).astype(np.int16).tobytes(),
                          "mime_type": "audio/pcm"})
        except asyncio.QueueFull:
            pass

    async def _flush_pending_vision(self) -> bool:
        """Send a captured frame immediately after its tool response.

        The frame is already in hand by the time `screen_process` returns — the
        capture happened inside the tool call. The old flow still made the model
        speak a turn first and only injected the image on that turn's
        turn_complete, which cost a whole extra round trip AND produced two
        spoken answers: one improvised without the picture, then the real one.
        Sending it here means the model has the tool result and the image before
        it generates anything, so the user gets one answer, sooner.
        """
        if not (self._pending_vision and self.session):
            return False

        import base64 as _b64
        img_b, mime_t, question, angle = self._pending_vision
        self._pending_vision = None
        b64 = _b64.b64encode(img_b).decode("ascii")
        print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

        # Label the source. Without it the image arrives carrying nothing but
        # the user's own sentence, and a screenshot of this app — which has a
        # face in the middle of it — got read as a photo of the user. What the
        # label *means* is explained once, in the generated [SELF] block.
        src = ("[IMAGE SOURCE: WEBCAM]" if angle == "camera"
               else "[IMAGE SOURCE: SCREEN CAPTURE]")
        await self.session.send_client_content(
            turns={"role": "user", "parts": [
                {"inline_data": {"mime_type": mime_t, "data": b64}},
                {"text": f"{src}\n\n{question}"},
            ]},
            turn_complete=True,
        )

        if self._vision_cam_active:
            # Camera: stay busy until JARVIS has finished speaking the answer,
            # then close the preview.
            self._vision_cam_active    = False
            self._vision_close_pending = True
        else:
            self._vision_busy = False
        return True

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    # ── go_away ──────────────────────────────────────────────
                    # The server announces its own deadline before it acts on
                    # it (Google ends Live sockets after ~10 minutes and then
                    # closes with 1011). Acting on the notice — moving to a new
                    # socket on the resumption handle while the conversation is
                    # idle — turns an expected server restart from a crash into
                    # a reconnect the user never sees.
                    _ga = getattr(response, "go_away", None)
                    if _ga is not None and not self._goaway_pending:
                        self._goaway_pending = True
                        self._goaway_task = asyncio.create_task(
                            self._on_go_away(getattr(_ga, "time_left", None)))

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        elif self._interview_mode and not self._speak_answers:
                            # Interview mode: the answer is text on the
                            # suggestions panel. Playing it would be heard by
                            # the interviewer, and with SYSTEM SOUND on it
                            # would feed back into the loopback the model is
                            # listening to. Config key speak_answers=true
                            # puts the audio back.
                            pass
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            # A turn that involves a tool call passes through
                            # several turn_completes, and the API re-sends the
                            # tail of the transcript across them. Comparing only
                            # against the previous chunk missed that — once
                            # out_buf had been flushed and emptied, the repeat
                            # sailed straight back in, which logged the answer
                            # twice AND made the avatar mouth it twice.
                            if txt and not _is_repeat_chunk(txt, out_buf):
                                out_buf.append(txt)
                                # Stream suggestions in real-time as they arrive
                                _joined = " ".join(out_buf)
                                self.ui.set_suggestion(_joined)
                                # Same words to Telegram — the worker coalesces
                                # and rate-limits them, this just queues.
                                self._tg_enqueue("push", _joined)
                                # Hand the words to the mouth as they arrive, so
                                # the avatar can form the consonants the audio
                                # alone cannot show. Pure string work — it adds
                                # nothing measurable to the response path.
                                self._visemes.feed_text(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                if not in_buf:
                                    self.ui.clear_suggestion()
                                    # The interviewer cut into an in-flight
                                    # answer: the panel is blanked, so blank
                                    # Telegram too — the next answer starts a
                                    # fresh message rather than editing this one.
                                    if out_buf:
                                        self._tg_enqueue("delete")
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                _partial = " ".join(out_buf).strip()
                                if _partial:
                                    # The panel keeps showing the cut-off words;
                                    # mark them as cut on Telegram so the phone
                                    # never looks like a finished answer.
                                    self._tg_enqueue("end", _partial + " …")
                                in_buf  = []
                                out_buf = []
                                self._visemes.reset()
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self._last_out_logged = ""   # new exchange
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            # Second line of defence: even if a repeat slips
                            # into a *fresh* buffer after a flush, never log the
                            # same answer (or a tail of it) twice in a row.
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                self._last_out_logged = full_out
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            # Retire the live mirror whether or not there is text
                            # to show: with text it edits the streamed message to
                            # the exact final words (or plain-sends when nothing
                            # streamed); empty it just closes the message so the
                            # next answer starts fresh. Covers the blanked
                            # repeat-tail above, where a plain send would have
                            # posted the same answer twice.
                            self._tg_enqueue("end", full_out)
                            out_buf = []

                            if self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
                        await self._flush_pending_vision()
        except Exception as e:
            # A 1011 is the server ending the socket on purpose — the run loop
            # reports it as the reconnect it is, so the full traceback here is
            # only noise that buries the real errors.
            print(f"[JARVIS] ❌ Recv: {e}")
            if "1011" not in str(e) and "internal error" not in str(e).lower():
                traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[JARVIS] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        # Ask the device how far behind the speakers actually are, rather than
        # assuming. This is what the echo tail is sized from, so a machine with a
        # large audio buffer gets a correspondingly longer guard — and one with a
        # tiny buffer is not penalised with a delay it does not need.
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[JARVIS] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Drive the HUD waveform and the avatar's mouth from JARVIS's
                # own voice. The batch is up to 200 ms long, so we hand over a
                # *schedule* of 20 ms viseme frames instead of a single averaged
                # level and let the HUD play it out in step with the audio.
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = _pcm_visemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    # When does this batch become audible? The stream was
                    # started at launch and its callback has been pulling
                    # silence ever since, so the first bytes of a reply reach
                    # the speaker about one callback period later — NOT one
                    # buffer later. `stream.latency` reports the buffer's
                    # capacity, which is how much can be queued ahead, and on
                    # Windows that is commonly 300-500 ms. Anchoring on it put
                    # the entire schedule a buffer late; that is the half second
                    # of lag, and it grew with whatever the device reported.
                    #
                    # After the anchor nothing needs measuring: the device
                    # consumes at exactly realtime, so each batch sounds one
                    # batch-duration after the one before it. The cursor is
                    # re-anchored only when it leaves the range physically
                    # possible — behind `now` means the device drained and this
                    # batch starts a fresh stretch of speech, while further
                    # ahead than the buffer can hold means it has drifted.
                    now = time.time()
                    horizon = self._out_latency + _CURSOR_SLACK
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    # Advance by the batch's own duration whether or not it
                    # yielded frames, so a block too short to analyse cannot
                    # shift everything after it out of step with the audio.
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        frames = self._visemes.frames(frames, hop)
                        self.ui.push_visemes(frames, hop, at)
                        # Barge-in needs to know what we are playing, not just
                        # how loud: the guard subtracts this from the microphone.
                        self._out_level = max(f[0] for f in frames)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE,
                                               self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        # The briefing fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p1}]},
            turn_complete=True,
        )
        print("[JARVIS] Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = (f" Speak in {lang} unless the user has since "
                            f"spoken another language, in which case use theirs."
                            if lang else "")

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=8.0)
                except Exception as e:
                    self.ui.write_log(f"SYS: News fetch timed out/failed: {e!r}")
                    news_text = ""

                if not self.session:
                    return

                failed = (not news_text) or news_text.startswith(
                    ("No news found", "Search failed", "Please provide")
                )
                if not failed:
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    self.ui.write_log(
                        f"SYS: News unavailable — backend returned: {news_text[:120]!r}"
                    )
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": p2}]},
                    turn_complete=True,
                )
                print("[JARVIS] Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                print(f"[JARVIS] Briefing phase 2 failed: {e}")
                self.ui.write_log("SYS: Could not fetch the news for the briefing.")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from core import gemini
            summary = await asyncio.to_thread(
                gemini.text, prompt, gemini.SMART, None, 30_000,
            )
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session and self._awake:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            print("[JARVIS] Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session or not self._awake:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                print("[JARVIS] Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    # A remote command is deliberate control and the phone user
                    # has no desktop WAKE button — so it wakes JARVIS if asleep.
                    if self._wake_enabled and not self._awake:
                        self.wake(reason="remote command")
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        global LIVE_MODEL
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        # System-sound supervision is session-independent (it gates on
        # ui.system_sound and pushes into whichever out_queue the current
        # session owns), so it also lives for the whole lifetime — starting
        # it per-session made every reconnect stack a supervisor and cancel
        # the previous one mid-capture, orphaning its child process.
        asyncio.create_task(self._listen_system_audio())

        while True:
            # Defaults for the handler below: a failure before the session is
            # built must not turn into NameError inside its own except block.
            _resumed_with = False
            live_model    = LIVE_MODEL
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                # Pick the rung to open the conversation on. A model resting
                # off a quota limit is skipped; the name is published back to
                # LIVE_MODEL so plugins follow whatever is actually in use.
                LIVE_MODEL = _gemini.live_model()
                live_model = LIVE_MODEL
                print(f"[JARVIS] Live model: {live_model}")

                _resumed_with = self._resume_handle is not None
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries proactive audio; if it gets rejected we fall
                # back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=live_model, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False
                    self._goaway_pending       = False
                    # A session that connected is proof the last failure is
                    # over: the backoff must start again from its baseline, or
                    # one bad evening leaves every later reconnect waiting the
                    # capped delay forever.
                    self._conn_backoff         = 3

                    print("[JARVIS] Connected.")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    # Wake word: if enabled, come up ASLEEP (mic gated, silent)
                    # until the user says "Hey Jarvis" or taps wake in the UI.
                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log(f"SYS: {self._asst_name} online — sleeping. Say 'Hey Jarvis' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log(f"SYS: {self._asst_name} online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    # NOTE: _listen_system_audio is deliberately NOT here — it
                    # starts once in run(). Inside this TaskGroup every
                    # reconnect spawned a new supervisor and cancelled the old
                    # one mid-capture, orphaning its child process.
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_sleep_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                #
                # `err_str` is the UNWRAPPED message: str(ExceptionGroup) is
                # only "unhandled errors in a TaskGroup", and testing that for
                # the word "handle" matched "unhandled" — which reported this
                # branch on every crash, threw away a good handle and lost the
                # conversation each time.
                err_str = _error_text(e)
                low     = err_str.lower()

                if _resumed_with and _is_handle_rejection(err_str):
                    print("[JARVIS] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # 1011 is Google's own close: Live sockets are ended after about
                # ten minutes (a go_away notice comes first) and the server can
                # also hiccup on its own. It says nothing about the handle or
                # this app — reconnect on the same handle and the conversation
                # carries on, so it is logged as what it is, not as a crash.
                if "1011" in err_str or "internal error" in low:
                    print("[JARVIS] ⚠️ Server closed the session (1011) — reconnecting"
                          + (" with the conversation intact." if self._resume_handle else "."))
                    self.ui.write_log(
                        "SYS: Connection dropped by the server — reconnecting"
                        + (" with the conversation intact." if self._resume_handle else "."))
                    self._conn_backoff = 3
                else:
                    print(f"[JARVIS] Error ({type(e).__name__}): {err_str}")
                    traceback.print_exc()

                # Out of quota, or this model is not available to this key —
                # step down the ladder and reconnect straight away. This is the
                # difference between "JARVIS is quieter today" and "JARVIS does
                # not start today": one model means one daily limit, and the
                # limit always arrives mid-conversation.
                if _gemini.note_live_failure(live_model, err_str):
                    nxt = _gemini.live_model()
                    self.ui.write_log(
                        f"SYS: Switching to {nxt.split('/')[-1]} — the previous "
                        f"model is out of quota."
                        if nxt != live_model else
                        "SYS: Every live model is rate-limited — retrying.")
                    self._conn_backoff = 0 if nxt != live_model else 15
                    if nxt == live_model:
                        await asyncio.sleep(self._conn_backoff)
                    continue

                # Turn-taking / media / thinking knobs rejected by the server
                # (preview API drift) — drop them first, because they are the
                # newest fields and the cheapest to lose. Proactive audio is
                # tried again on the next pass if the error persists.
                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "media_resolution" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[JARVIS] Live tuning rejected — reconnecting without it.")
                    continue

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    self._conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # The go_away watcher outlives the TaskGroup (it is created
                # outside it, on purpose, so it can ask for a reconnect). Once
                # the session is gone it has nothing left to protect — and if
                # it fired later it would set an event the NEXT session reads,
                # tearing down a perfectly good connection on sight.
                _ga_task = getattr(self, "_goaway_task", None)
                if _ga_task is not None:
                    _ga_task.cancel()
                    self._goaway_task = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

# ── License gate (client/licensing.py: Ed25519 offline key + activation) ──

_LICENSE: "tuple[str, str, str] | None" = None   # (key, kid, device_id) once past the gate


def _license_watchdog(kid, device_id, recheck_fn, minutes):
    """Soft revocation watchdog. recheck() -> False means the server revoked
    the key: warn and exit. None (network trouble) and True mean keep running
    — an offline launch must never be killed by a sleeping server."""
    while True:
        time.sleep(minutes * 60)
        try:
            result = recheck_fn(kid, device_id)
        except Exception:
            result = None
        if result is False:
            print("[license] Revoked on the license server — exiting.")
            try:
                from tkinter import messagebox
                messagebox.showerror(
                    "License revoked",
                    "This license has been revoked.\n"
                    "Interview Assistant will now close.")
            except Exception:
                pass
            os._exit(1)


def _license_gate() -> bool:
    """Blocking paywall before JarvisUI comes up. True = licensed (watchdog
    running), False = the user quit without a valid key."""
    global _LICENSE
    try:
        client_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client")
        if client_dir not in sys.path:
            sys.path.insert(0, client_dir)
        from licensing import ensure_licensed, recheck as _recheck, RECHECK_MINUTES
    except Exception as exc:
        print(f"[license] licensing module unavailable: {exc}")
        try:
            from tkinter import messagebox
            messagebox.showerror("License error",
                                 f"Licensing module failed to load:\n{exc}")
        except Exception:
            pass
        return False
    result = ensure_licensed()
    if result is None:
        print("[license] No valid license — exiting.")
        return False
    _LICENSE = result
    key, kid, device_id = result
    threading.Thread(target=_license_watchdog,
                     args=(kid, device_id, _recheck, RECHECK_MINUTES),
                     daemon=True, name="license-watchdog").start()
    print(f"[license] active kid={kid} device={device_id}")
    return True


def main():
    # A native access violation (the recurring "dies with no traceback"
    # failure) kills Python before it can print anything useful. faulthandler
    # installs a Windows exception handler that dumps the Python stack of the
    # faulting thread to stderr — turning a silent crash into a located one.
    try:
        import faulthandler
        faulthandler.enable()
    except Exception:
        pass
    # Dev escape hatch: --no-license skips the paywall entirely.
    if "--no-license" not in sys.argv:
        if not _license_gate():
            return
    ui = JarvisUI("face.png")
    box = {}

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        box["jarvis"] = jarvis
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()
    # The window is gone. Stop the loopback capture process — it is isolated,
    # so even an abrupt exit here is safe, but a clean stop costs nothing.
    ui.system_sound = False
    jarvis = box.get("jarvis")
    if jarvis is not None:
        proc = getattr(jarvis, "_sys_proc", None)
        if proc is not None:
            try:
                proc.terminate()
                proc.join(2.0)
            except Exception:
                pass

if __name__ == "__main__":
    main()