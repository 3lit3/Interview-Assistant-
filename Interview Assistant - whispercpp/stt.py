from __future__ import annotations

import asyncio
import logging
import numpy as np

from config import Config
from audio_capture import AudioCapture

log = logging.getLogger("stt")

try:
    from pywhispercpp.model import Model as CppModel

    _HAS_CPP = True
except Exception:  # pragma: no cover - optional dependency
    _HAS_CPP = False


class VADSegmenter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.samplerate = cfg.samplerate
        self.frame_samples = int(cfg.samplerate * cfg.vad_frame_ms / 1000)
        self.silence_frames = max(1, int(cfg.silence_ms / cfg.vad_frame_ms))
        self.min_frames = int(cfg.min_utterance_ms / cfg.vad_frame_ms)
        self.max_frames = int(cfg.max_utterance_ms / cfg.vad_frame_ms)
        self.partial_frames = max(1, int(cfg.partial_interval * 1000 / cfg.vad_frame_ms))
        self.noise_floor: float | None = None
        self._buf = np.zeros(0, dtype="<i2")
        self._cur: list[np.ndarray] = []
        self._in_speech = False
        self._silence_count = 0
        self._speech_frames = 0
        self._since_partial = 0

    def _is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2) + 1e-9))
        if self.noise_floor is None:
            self.noise_floor = rms
        elif not self._in_speech:
            self.noise_floor = 0.999 * self.noise_floor + 0.001 * rms
        threshold = max(self.noise_floor * self.cfg.vad_noise_multiplier, self.cfg.vad_min_rms)
        return rms > threshold

    def feed(self, pcm: np.ndarray):
        self._buf = np.concatenate([self._buf, pcm])
        utterances: list[np.ndarray] = []
        partial: np.ndarray | None = None
        while len(self._buf) >= self.frame_samples:
            frame = self._buf[: self.frame_samples]
            self._buf = self._buf[self.frame_samples :]
            is_speech = self._is_speech(frame)

            if is_speech:
                if not self._in_speech:
                    self._in_speech = True
                    self._cur = []
                    self._speech_frames = 0
                    self._since_partial = 0
                    log.info("VAD: speech start")
                self._cur.append(frame)
                self._speech_frames += 1
                self._silence_count = 0
                self._since_partial += 1
                if (
                    self._since_partial >= self.partial_frames
                    and self._speech_frames >= self.min_frames
                ):
                    partial = np.concatenate(self._cur)
                    self._since_partial = 0
            else:
                if self._in_speech:
                    self._silence_count += 1
                    if self._silence_count >= self.silence_frames:
                        self._flush(utterances)
                if self._in_speech and self._speech_frames >= self.max_frames:
                    self._flush(utterances)
        return utterances, partial

    def _flush(self, out: list[np.ndarray]):
        if self._speech_frames >= self.min_frames and self._cur:
            out.append(np.concatenate(self._cur))
        self._cur = []
        self._in_speech = False
        self._silence_count = 0
        self._speech_frames = 0
        self._since_partial = 0


class STTEngine:
    def __init__(
        self,
        cfg: Config,
        audio: AudioCapture,
        transcript_queue: asyncio.Queue[str],
        partial_queue: asyncio.Queue[str] | None = None,
    ):
        self.cfg = cfg
        self.audio = audio
        self.transcript_queue = transcript_queue
        self.partial_queue = partial_queue
        self.segmenter = VADSegmenter(cfg)
        self.model = None
        self.backend = None

    def _load_model(self):
        if self.model is not None:
            return
        if self.cfg.stt_backend == "whispercpp" and _HAS_CPP:
            self.backend = "cpp"
            self.model = CppModel(self.cfg.stt_model, n_threads=self.cfg.stt_threads)
            log.info("STT backend: whisper.cpp (pywhispercpp)")
            return
        if self.cfg.stt_backend == "whispercpp" and not _HAS_CPP:
            log.warning("pywhispercpp not installed; falling back to faster-whisper.")
        self.backend = "fw"
        from faster_whisper import WhisperModel

        self.model = WhisperModel(
            self.cfg.stt_model,
            device=self.cfg.stt_device,
            compute_type=self.cfg.stt_compute_type,
        )
        log.info("STT backend: faster-whisper")

    def _transcribe(self, pcm: np.ndarray) -> str:
        if self.backend == "cpp":
            return self._transcribe_cpp(pcm)
        return self._transcribe_fw(pcm)

    def _transcribe_fw(self, pcm: np.ndarray) -> str:
        audio_f = pcm.astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio_f,
            language="en",
            vad_filter=False,
            beam_size=1,
            best_of=1,
            temperature=[0.0, 0.5, 1.0],
            condition_on_previous_text=False,
            compression_ratio_threshold=2.0,
            log_prob_threshold=-0.5,
        )
        texts: list[str] = []
        probs: list[float] = []
        for seg in segments:
            t = seg.text.strip()
            if t:
                texts.append(t)
            probs.append(getattr(seg, "no_speech_prob", 0.0))
        text = " ".join(texts).strip()
        if not text:
            return ""
        if probs and (sum(probs) / len(probs) > self.cfg.stt_no_speech_threshold):
            return ""
        if self._is_repetitive(text):
            return ""
        return text

    def _transcribe_cpp(self, pcm: np.ndarray) -> str:
        audio_f = pcm.astype(np.float32) / 32768.0
        try:
            segments = self.model.transcribe(
                audio_f,
                language="en",
                n_threads=self.cfg.stt_threads,
                no_context=True,
                single_segment=True,
            )
        except Exception as exc:
            log.exception("whisper.cpp transcribe failed: %s", exc)
            return ""
        text = " ".join(getattr(s, "text", "") for s in segments).strip()
        if not text:
            return ""
        if self._is_repetitive(text):
            return ""
        return text

    @staticmethod
    def _is_repetitive(text: str) -> bool:
        words = text.lower().split()
        if len(words) < 8:
            return False
        if len(set(words)) <= 2:
            return True
        for n in (3, 4):
            grams = [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]
            if grams and grams.count(max(set(grams), key=grams.count)) >= 3:
                return True
        return False

    async def run(self):
        self._load_model()
        loop = asyncio.get_event_loop()
        chunks = 0
        while True:
            chunk = await self.audio.get_chunk()
            chunks += 1
            if chunks == 1:
                log.info("Audio stream live (device=%s); receiving chunks", self.cfg.device_index)
            utterances, _partial = self.segmenter.feed(chunk)
            # Partial VAD segments are intentionally NOT streamed to Telegram — only
            # finalized utterances below are processed, to avoid preview/API latency.
            for utt in utterances:
                text = await loop.run_in_executor(None, self._transcribe, utt)
                if text:
                    await self.transcript_queue.put(text)
                else:
                    log.info("VAD: utterance dropped by filters (frames=%d)", len(utt))
