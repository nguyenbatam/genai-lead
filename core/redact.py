"""Mask secrets and long account numbers before a model or a log sees them."""

from __future__ import annotations

import re

_ACCOUNT = re.compile(r"\b\d{12,}\b")
_SECRET = re.compile(r"\bsk_[A-Za-z0-9_\-]{8,}\b")
MASK = "[redacted]"


def redact(text: str) -> str:
    return _SECRET.sub(MASK, _ACCOUNT.sub(MASK, text))
