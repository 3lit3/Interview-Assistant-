from __future__ import annotations

import re

QUESTION_PREFIX = re.compile(
    r"^\s*(what|why|how|when|where|who|whom|which|whose"
    r"|can|could|will|would|shall|should"
    r"|do|does|did|are|is|am|have|has|had|may|might|must"
    r"|tell me|walk me through|explain|describe|give me|share|name|list|say|introduce"
    r"|what's|who's|how's|where's|when's|why's|can you|could you|would you"
    r"|do you|are you|is it|should i|what kind|what type|what are|how do|why do)\b",
    re.IGNORECASE,
)


def is_question(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.endswith("?") or "?" in t:
        return True
    if QUESTION_PREFIX.search(t):
        return True
    return False
