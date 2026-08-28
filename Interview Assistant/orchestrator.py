from __future__ import annotations

import asyncio
import logging

from audio_capture import AudioCapture
from config import Config, load_config
from context import ContextManager
from cv_loader import build_system_prompt, load_cv
from gate import is_question
from llm import LLMClient
from stt import STTEngine
from telegram import TelegramSender

log = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.audio = AudioCapture(cfg)
        self.transcript_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self.partial_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        cv_text = load_cv(cfg.cv_path)
        system_prompt = build_system_prompt(cfg.system_prompt, cv_text)
        self.context = ContextManager(system_prompt, cfg.context_window)
        self.llm = LLMClient(cfg, self.context)
        self.telegram = TelegramSender(cfg)
        self.stt = STTEngine(cfg, self.audio, self.transcript_queue, self.partial_queue)
        self._running = False
        self._transcript_msg_id: int | None = None
        self._last_partial: str = ""

    async def run(self):
        self._running = True
        await self.audio.start()
        log.info("Audio capture started; loading STT model (first run downloads weights)...")
        await self.telegram.notify("🟡 Interview Assistant starting — loading STT model…")
        await asyncio.get_event_loop().run_in_executor(None, self.stt._load_model)
        mode = "microphone (transcript shown)" if self.cfg.echo_transcript else "system audio"
        cv_note = " · CV loaded" if self.cfg.cv_path else ""
        await self.telegram.notify(f"🟢 Assistant live — source: {mode}{cv_note}. Speak now.")
        log.info("Pipeline live. Speak into the microphone...")
        stt_task = asyncio.create_task(self.stt.run())
        pipe_task = asyncio.create_task(self._pipeline())
        partial_task = asyncio.create_task(self._partial_consumer())
        try:
            await asyncio.gather(stt_task, pipe_task, partial_task)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.shutdown()

    async def _partial_consumer(self):
        while self._running:
            try:
                text = await asyncio.wait_for(self.partial_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not self.cfg.echo_transcript:
                continue
            if self._transcript_msg_id is None:
                self._transcript_msg_id = await self.telegram.begin_transcript()
            await self.telegram.update_transcript(
                self._transcript_msg_id, f"🎙 {text}▌"
            )

    async def _pipeline(self):
        while self._running:
            try:
                utterance = await asyncio.wait_for(
                    self.transcript_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            log.info("USER: %s", utterance)
            if self.cfg.question_gate and not self.cfg.echo_transcript and not is_question(utterance):
                log.info("Dropped (not a question): %s", utterance)
                continue
            try:
                if self.cfg.echo_transcript:
                    if self._transcript_msg_id is not None:
                        await self.telegram.update_transcript(
                            self._transcript_msg_id, f"🎙 {utterance}"
                        )
                        self._transcript_msg_id = None
                    else:
                        await self.telegram.send_user_turn(utterance)
                reply = self.llm.stream_reply(utterance)
                await self.telegram.stream_reply(reply)
            except Exception as exc:
                log.exception("Failed to handle utterance: %s", exc)
                await self.telegram.notify(f"⚠️ Error: {exc}")

    async def shutdown(self):
        self._running = False
        await self.audio.stop()
        await self.telegram.notify("🔴 Assistant stopped.")
        await self.llm.close()
        await self.telegram.close()


def main():
    cfg = load_config()
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
