from __future__ import annotations

import json

import httpx

from config import Config
from context import ContextManager


class LLMClient:
    def __init__(self, cfg: Config, context: ContextManager):
        self.cfg = cfg
        self.context = context
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    async def stream_reply(self, user_text: str):
        self.context.add_user(user_text)
        messages = self.context.build_messages()

        if self.cfg.llm_backend == "ollama":
            token_gen = self._stream_ollama(messages)
        else:
            token_gen = self._stream_openai(messages)

        full = ""
        async for tok in token_gen:
            full += tok
            yield tok

        self.context.add_assistant(full.strip())

    async def _stream_ollama(self, messages):
        payload = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": self.cfg.llm_temperature,
                "num_predict": self.cfg.llm_max_tokens,
            },
        }
        async with self.client.stream(
            "POST", f"{self.cfg.llm_base_url}/api/chat", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                delta = data.get("message", {}).get("content", "")
                if delta:
                    yield delta
                if data.get("done"):
                    break

    async def _stream_openai(self, messages):
        payload = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "stream": True,
            "temperature": self.cfg.llm_temperature,
            "max_tokens": self.cfg.llm_max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.cfg.llm_api_key}"}
        if self.cfg.llm_base_url.rstrip("/").endswith("openrouter.ai/api/v1"):
            headers["HTTP-Referer"] = "http://localhost/interview-assistant"
            headers["X-Title"] = "Interview Assistant"
        url = f"{self.cfg.llm_base_url.rstrip('/')}/chat/completions"
        async with self.client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                delta = data["choices"][0]["delta"].get("content", "")
                if delta:
                    yield delta

    async def close(self):
        await self.client.aclose()
