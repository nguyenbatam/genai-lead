"""Instruction-shaped lines are data. The regex is not the authority boundary."""

from __future__ import annotations

import re

_SHAPES = (
    r"ignore\s+(?:all\s+)?(?:your\s+|the\s+)?(?:previous|prior|above)\s+instructions",
    r"(?:disregard|forget|override)\s+(?:all\s+)?(?:your\s+|the\s+)?"
    r"(?:previous|prior|above|system)\s+(?:instructions?|prompts?|rules?|guidelines?)",
    r"you\s+are\s+now\b",
    r"reveal\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|secrets?)",
    r"\bexfil\b",
    r"bỏ\s+qua\s+(?:mọi|tất\s+cả|các)\s+(?:chỉ\s+dẫn|hướng\s+dẫn)",
)
_INJECTION = re.compile("|".join(f"(?:{shape})" for shape in _SHAPES), re.IGNORECASE)
_MARKER = re.compile(r"<<\s*/?\s*(?:data|end)\s*:[^>\n]*>>", re.IGNORECASE)
WITHHELD = "[withheld: instruction-like text]"


def allows(text: str) -> bool:
    return _INJECTION.search(text) is None


def scrub(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    return "\n".join(line if allows(line) else WITHHELD for line in lines)


def neutralize_markers(text: str) -> str:
    return _MARKER.sub("[marker]", text)


def spotlight(label: str, body: str) -> str:
    safe = neutralize_markers(scrub(body))
    return f"<<data:{label}>>\n{safe}\n<<end:{label}>>"
