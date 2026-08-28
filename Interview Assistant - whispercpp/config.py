from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


@dataclass
class Config:
    samplerate: int = 16000
    channels: int = 1
    frames_per_buffer: int = 512
    device_index: int | None = None
    loopback: bool = False

    stt_backend: str = "whispercpp"
    stt_model: str = "tiny.en"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_threads: int = 4

    vad_aggressiveness: int = 2
    vad_frame_ms: int = 30
    silence_ms: int = 800
    partial_interval: float = 0.4
    min_utterance_ms: int = 400
    max_utterance_ms: int = 15000
    stt_no_speech_threshold: float = 0.6
    vad_noise_multiplier: float = 2.0
    vad_min_rms: float = 250.0
    question_gate: bool = True

    llm_backend: str = "openai"
    llm_model: str = "openai/gpt-4.1-mini"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = field(
        default_factory=lambda: _env("OPENROUTER_API_KEY", "") or _env("LLM_API_KEY", "")
    )
    llm_temperature: float = 0.7
    llm_max_tokens: int = 400

    system_prompt: str = (
    	"You are a real-time interview answer assistant. Your job is to generate "
    	"the exact answer the candidate should give to the interviewer's most recent "
   	"question. Do not ask follow-up questions. Do not provide suggestions, "
    	"coaching, explanations, analysis, or multiple answer options. "
    	"Do not mention that you are an AI assistant. "
    	"Return only the candidate's answer, written naturally in first person, "
    	"as if the candidate is speaking directly to the interviewer. "
    	"Make the answer clear, confident, professional, and relevant to the "
    	"specific question. Use information from the candidate's CV and provided "
   	"context when relevant, but do not invent experience, skills, achievements, "
    	"or facts. "
    	"For behavioral questions, structure the response naturally using the "
    	"candidate's real experience and emphasize actions and outcomes. "
    	"For technical questions, give a direct and accurate explanation. "
    	"Keep answers concise and interview-ready, typically under 100 words "
    	"unless the question clearly requires more detail. "
    	"IMPORTANT: Output ONLY the answer the candidate should say. Never ask "
    	"the candidate a question and never explain how they should answer."
    )

    echo_transcript: bool = True
    cv_path: str = ""
    context_window: int = 6

    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))
    telegram_edit_interval: float = 0.35
    telegram_typing: bool = True

    log_level: str = "INFO"


def load_config() -> Config:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    return Config()
