# Interview Assistant

A local, real-time interview answer assistant. It captures microphone or system/speaker
audio, transcribes it with a streaming STT engine, keeps a short rolling context, sends the
interviewer's question to an LLM (OpenRouter GPT-4.1-mini), and delivers the candidate's
answer straight to Telegram.

This repository contains **two variants**:

| Folder | Backend | Notes |
| --- | --- | --- |
| `Interview Assistant/` | `faster-whisper` | Stable, well-tested baseline. |
| `Interview Assistant - whispercpp/` | `whisper.cpp` (via `pywhispercpp`) | Lower-latency backend plus the extra features below. **This is the actively developed variant.** |

## How it works

1. **Capture** — `AudioCapture` reads 16 kHz mono audio from a chosen input device
   (microphone, or a loopback input such as *Stereo Mix* / *VB-Audio Virtual Cable* for
   system/speaker audio).
2. **Voice activity detection** — a lightweight numpy energy-based VAD segments speech and
   drops silence, with hallucination guards on the STT output.
3. **STT** — whisper.cpp (`pywhispercpp`) by default, with automatic fallback to
   faster-whisper if `pywhispercpp` is not installed.
4. **Context + LLM** — the last few turns plus your CV are sent to OpenRouter
   (`openai/gpt-4.1-mini`); the model returns only the answer the candidate should say.
5. **Delivery** — the answer is sent to Telegram as a single message. In microphone mode the
   transcript is also shown; in system-audio mode only the assistant reply is sent.

## Features (both variants)

- Microphone **or** system/loopback capture.
- Streaming STT with hallucination guards (`no_speech_prob`, compression-ratio, log-prob,
  repetition filters).
- Optional **question gate** (system-audio mode only): non-questions are ignored so the
  assistant only answers real interviewer questions.
- **CV injection**: load a `.txt` / `.pdf` / `.docx` CV; it is injected into the system
  prompt. Override the base persona with `system_prompt.txt` next to the script.

## Extra features (`Interview Assistant - whispercpp/`)

- **`--test-capture`** — records 5 seconds from the selected device and prints peak RMS,
  mean RMS, estimated noise floor, VAD threshold, and the percentage of frames above
  threshold. Use this to verify routing/device selection before a live run
  (no STT/LLM/Telegram is started).
- **Runtime CV reload over Telegram** — while running, message the bot:
  - send a **document** (`.txt`/`.pdf`/`.docx`) → CV is re-parsed and the system prompt
    rebuilt live;
  - `/cv <path>` → reload a CV from a local file;
  - `/status` → show mode, CV state, context turns, queue size;
  - `/reset` → clear the conversation context.

## Setup — whispercpp variant (recommended)

```powershell
cd "Interview Assistant - whispercpp"
python -m venv win_env
.\win_env\Scripts\Activate.ps1
pip install -r requirements_whispercpp.txt

# 1) list input devices
python main.py --list-devices

# 2) verify audio routing (records 5s, prints RMS stats, then exits)
python main.py --test-capture --device <DEVICE_INDEX>

# 3) run live
python main.py --device <DEVICE_INDEX>
```

Telegram flags (optional): `--mic`, `--loopback`, `--silence-ms`, `--stt-model`,
`--llm-model`, `--chat-id`, `--cv <path>`.

## Setup — stable variant

```powershell
cd "Interview Assistant"
python -m venv win_env
.\win_env\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py --device <DEVICE_INDEX>
```

## Configuration

Credentials are read from a `.env` file in each project folder (never committed):

```
OPENROUTER_API_KEY=your_openrouter_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
```

All other settings (model, VAD thresholds, latency, context window, Telegram behavior) live
in `config.py` and can be overridden via the corresponding `.env` keys or CLI flags.

## Notes

- For **system/speaker** capture, route your meeting/playback audio into a loopback input
  device (enable *Stereo Mix*, or set playback to a *VB-Audio Virtual Cable*). A microphone
  must NOT be set to "Listen to this device", or it will leak into the cable.
- The default LLM is `openai/gpt-4.1-mini` through OpenRouter; change `llm_model` in
  `config.py` if desired.
- The first STT run downloads model weights automatically.
