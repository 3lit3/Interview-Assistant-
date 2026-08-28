from __future__ import annotations

import logging
import os

log = logging.getLogger("cv_loader")


def load_cv(path: str) -> str:
    if not path:
        return ""
    if not os.path.exists(path):
        log.warning("CV path does not exist: %s", path)
        return ""

    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        return _read_text(path)

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            log.warning("pypdf not installed; cannot parse PDF CV. `pip install pypdf`.")
            return ""
        reader = PdfReader(path)
        return "\n".join((p.extract_text() or "") for p in reader.pages)

    if ext in (".docx", ".doc"):
        try:
            import docx
        except ImportError:
            log.warning("python-docx not installed; cannot parse DOCX CV. `pip install python-docx`.")
            return ""
        document = docx.Document(path)
        return "\n".join(p.text for p in document.paragraphs)

    return _read_text(path)


def build_system_prompt(base: str, cv_text: str) -> str:
    if not cv_text:
        return base
    trimmed = cv_text.strip()[:8000]
    return (
        base
        + "\n\nThe candidate's CV is provided below. Use it to answer interview "
        "questions accurately and tailor suggestions to their experience:\n\n"
        + trimmed
    )


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read()
