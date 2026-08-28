from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    role: str
    content: str


class ContextManager:
    def __init__(self, system_prompt: str, window: int):
        self.system = system_prompt
        self.window = window
        self.turns: list[Turn] = []

    def add_user(self, text: str):
        self.turns.append(Turn("user", text))
        self._trim()

    def add_assistant(self, text: str):
        self.turns.append(Turn("assistant", text))
        self._trim()

    def _trim(self):
        if len(self.turns) > self.window * 2:
            self.turns = self.turns[-(self.window * 2) :]

    def set_system(self, text: str):
        self.system = text

    def reset(self):
        self.turns = []

    def build_messages(self) -> list[dict]:
        messages = [{"role": "system", "content": self.system}]
        for t in self.turns:
            messages.append({"role": t.role, "content": t.content})
        return messages
