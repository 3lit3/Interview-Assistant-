from __future__ import annotations

import re

# Anchored part (^ binds only to the first group) — weak markers that are only
# reliable at the START of an utterance. Unanchored part — markers that are
# still a question wherever they appear ("...but why should we hire you?").
QUESTION_RE = re.compile(
    r"^(?:what|why|how|when|where|who|whom|which|whose"
    r"|can|could|will|would|shall|should"
    r"|do|does|did|are|is|am|have|has|had|may|might|must"
    r"|tell me|walk me through|explain|describe|give me|share|name|list|say|introduce"
    r"|what's|who's|how's|where's|when's|why's|can you|could you|would you"
    r"|do you|are you|is it|should i|what kind|what type|what are|how do|why do)\b"
    r"|\b(?:why|when|where|whom|whose|how|what)\b"
    r"|\b(?:tell me|walk me through|give me|let me know|explain|describe"
    r"|introduce yourself|share your|name the|list the|let's hear|think about)\b",
    re.IGNORECASE,
)

# Leading words a transcriber prepends to an already-started sentence.
_LEAD_NOISE = re.compile(
    r"^\s*(?:so|okay|ok|well|right|yeah|yep|sure|great|good|nice|fine|alright"
    r"|now|then|first|second|third|next|finally|also|plus|and|but|well then"
    r"|alright then|anyway|anyways|hm|hmm|mm)[,.\-:;!?]*\s+",
    re.IGNORECASE,
)

# Words that only appear mid-sentence — if an utterance ENDS on one of these,
# whisper cut the sentence off before it was finished. Deliberately excludes
# object pronouns and phrasal-verb particles ("handle them", "find out",
# "that's all"), which legitimately end real sentences.
END_MARKERS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "but",
    "nor", "with", "that", "this", "these", "those", "is", "are", "was", "were",
    "be", "been", "being", "am", "have", "has", "had", "do", "does", "did",
    "can", "could", "will", "would", "shall", "should", "may", "might", "must",
    "your", "my", "our", "their", "its",
    "i", "we", "he", "she", "they",
    "by", "from", "if", "when", "while", "because", "than", "then",
    "what", "which", "who", "whom", "whose", "how", "why", "where",
    "not", "no", "very", "just", "also", "only",
}

# Utterance that STARTS with one of these is the tail of something already spoken.
CONTINUATION = {
    "and", "but", "or", "nor", "so", "then", "also", "plus", "because",
    "although", "though", "while", "whereas", "however", "therefore",
    "furthermore", "moreover", "besides", "instead", "otherwise", "hence",
    "thus", "yet", "another",
}

# Sounds / silence that whisper happily turns into the word "beep".
_NOISE_TOKENS = {
    "beep", "beeps", "beeping", "bing", "ding", "dong", "bloop", "boop",
    "buzz", "bzz", "tone", "tones", "chime", "chimes", "ring", "ringing",
    "buzzer", "alarm", "buzzing", "click", "tick", "ticks", "static", "hum",
    "silence", "noise", "notification", "skype", "zoom", "teams", "outlook",
    "uh", "um", "uhm", "erm", "er", "ah", "ahh", "eh", "hmm", "hm", "mhm",
    "mm", "mm-hmm", "mmhmm", "mmm", "huh", "ha", "haha", "ehm", "m-hm",
}

# Short acknowledgements that never need an answer.
_BACKCHANNEL = _NOISE_TOKENS | {
    "yeah", "yep", "yup", "ok", "okay", "alright", "right", "sure", "yes",
    "no", "nope", "fine", "good", "great", "nice", "cool", "wow", "thanks",
    "thank", "hello", "hi", "hey", "bye", "goodbye", "please", "correct",
    "exactly", "absolutely", "totally", "indeed", "welcome", "congrats",
    "congratulations",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def is_junk(text: str) -> bool:
    """True for beeps, tones, filler and backchannel — never worth answering."""
    toks = _tokens(text)
    if not toks:
        return True
    if all(t in _NOISE_TOKENS for t in toks):
        return True
    if len(toks) <= 2 and all(t in _BACKCHANNEL for t in toks):
        return True
    return False


def is_question(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if is_junk(t):
        return False
    if "?" in t:
        return True
    stripped = _LEAD_NOISE.sub("", t) or t
    if QUESTION_RE.search(stripped):
        return True
    # Truncated question ("...stored in that") still counts so the pipeline
    # keeps it instead of throwing the interviewer's question away.
    toks = _tokens(stripped)
    return bool(toks) and toks[0] in {
        "what", "why", "how", "when", "where", "who", "which", "whose",
    }


def is_complete(text: str) -> bool:
    """False when whisper almost certainly cut the utterance short."""
    t = text.strip()
    if not t:
        return True
    if t.endswith(("...", "…")) or t.endswith(","):
        return False
    if t[-1] not in ".?!":
        return False
    toks = _tokens(t)
    if not toks:
        return True
    # "Why?", "What if?" — too short to have been cut, don't hold them.
    if t.endswith("?") and len(toks) <= 2:
        return True
    if toks[-1] in END_MARKERS:
        return False
    if toks[0] in CONTINUATION and not is_question(t):
        return False
    return True
