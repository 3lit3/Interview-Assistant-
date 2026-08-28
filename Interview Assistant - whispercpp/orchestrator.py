from __future__ import annotations

import asyncio
import logging
import time

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
        self.base_system = cfg.system_prompt
        self.cv_text = load_cv(cfg.cv_path)
        system_prompt = build_system_prompt(self.base_system, self.cv_text)
        self.context = ContextManager(system_prompt, cfg.context_window)
        self.llm = LLMClient(cfg, self.context)
        self.telegram = TelegramSender(cfg)
        self.stt = STTEngine(cfg, self.audio, self.transcript_queue)
        self._running = False

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
        tasks = [
            asyncio.create_task(self.stt.run()),
            asyncio.create_task(self._pipeline()),
        ]
        if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            tasks.append(asyncio.create_task(self._telegram_listener()))
        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.shutdown()

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
                    await self.telegram.send_user_turn(utterance)
                reply = self.llm.stream_reply(utterance)
                await self.telegram.stream_reply(reply)
            except Exception as exc:
                log.exception("Failed to handle utterance: %s", exc)
                await self.telegram.notify(f"⚠️ Error: {exc}")

    def reload_cv(self, path: str) -> bool:
        cv = load_cv(path)
        if not cv:
            return False
        self.cv_text = cv
        self.context.set_system(build_system_prompt(self.base_system, cv))
        return True

    async def _telegram_listener(self):
        offset: int | None = None
        while self._running:
            updates = await self.telegram.get_updates(offset, timeout=20)
            for upd in updates:
                offset = upd.get("update_id", 0) + 1
                await self._handle_update(upd)
            if not updates:
                await asyncio.sleep(1)

    async def _handle_update(self, update):
        msg = update.get("message")
        if not msg:
            return
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if self.cfg.telegram_chat_id and str(self.cfg.telegram_chat_id) != chat_id:
            return
        text = (msg.get("text") or "").strip()
        doc = msg.get("document")
        if doc:
            file_id = doc.get("file_id")
            fp = await self.telegram.get_file_path(file_id)
            if fp:
                import tempfile
                from pathlib import Path

                suffix = Path(doc.get("file_name", "cv.bin")).suffix or ".bin"
                dest = Path(tempfile.gettempdir()) / f"cv_upload_{int(time.time())}{suffix}"
                ok = await self.telegram.download_file(fp, dest)
                if ok:
                    ok2 = self.reload_cv(str(dest))
                    await self.telegram.notify(
                        "✅ CV reloaded from document."
                        if ok2
                        else "⚠️ Could not parse that document as a CV (expect .txt/.pdf/.docx)."
                    )
                else:
                    await self.telegram.notify("⚠️ Failed to download document.")
            return
        if text.startswith("/cv "):
            path = text[4:].strip()
            ok = self.reload_cv(path)
            await self.telegram.notify(
                f"✅ CV reloaded from {path}." if ok else f"⚠️ Could not load CV at {path}."
            )
        elif text == "/status":
            await self.telegram.notify(self._status_text())
        elif text == "/reset":
            self.context.reset()
            await self.telegram.notify("🔄 Conversation context cleared.")

    def _status_text(self) -> str:
        mode = "mic" if self.cfg.echo_transcript else "system"
        cv = "loaded" if self.cv_text else "none"
        return (
            f"mode={mode} · cv={cv} · context_turns={len(self.context.turns)} · "
            f"transcript_q={self.transcript_queue.qsize()}"
        )

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
