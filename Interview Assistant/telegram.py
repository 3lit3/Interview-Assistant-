from __future__ import annotations

import asyncio
import time

import httpx

from config import Config


class TelegramSender:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def send_user_turn(self, text: str):
        if not self.cfg.telegram_chat_id:
            return
        await self._post(
            "sendMessage",
            {"chat_id": self.cfg.telegram_chat_id, "text": f"🎙 {text}"},
        )

    async def notify(self, text: str):
        if not self.cfg.telegram_chat_id:
            return
        await self._post(
            "sendMessage",
            {"chat_id": self.cfg.telegram_chat_id, "text": text},
        )

    async def begin_transcript(self) -> int | None:
        if not self.cfg.telegram_chat_id:
            return None
        resp = await self._post(
            "sendMessage",
            {"chat_id": self.cfg.telegram_chat_id, "text": "🎙 …"},
        )
        return resp.get("message_id")

    async def update_transcript(self, message_id: int, text: str):
        if not self.cfg.telegram_chat_id or message_id is None:
            return
        await self._post(
            "editMessageText",
            {
                "chat_id": self.cfg.telegram_chat_id,
                "message_id": message_id,
                "text": text,
            },
        )

    async def stream_reply(self, token_gen):
        if not self.cfg.telegram_chat_id:
            full = ""
            async for tok in token_gen:
                full += tok
            print("ASSISTANT:", full)
            return

        if self.cfg.telegram_typing:
            asyncio.create_task(self._typing_loop())

        full = ""
        async for tok in token_gen:
            full += tok

        if not full.strip():
            return
        for i in range(0, len(full), 4000):
            await self._post(
                "sendMessage",
                {"chat_id": self.cfg.telegram_chat_id, "text": full[i : i + 4000]},
            )

    async def _typing_loop(self):
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            await self._post(
                "sendChatAction",
                {"chat_id": self.cfg.telegram_chat_id, "action": "typing"},
            )
            await asyncio.sleep(4.0)

    async def _post(self, method: str, payload: dict):
        try:
            resp = await self.client.post(f"{self.base}/{method}", json=payload)
            return resp.json().get("result", {})
        except Exception:
            return {}

    async def close(self):
        await self.client.aclose()
