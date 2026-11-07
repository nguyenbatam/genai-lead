"""In-process spans. No vendor SDK — correlation id joins logs and these events."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

_lock = threading.Lock()
_spans: list[dict] = []
_MAX = 1_000


@dataclass
class Span:
    name: str
    attrs: dict[str, object] = field(default_factory=dict)
    start_ms: int = 0
    end_ms: int = 0

    def finish(self) -> None:
        if self.end_ms:
            return
        self.end_ms = int(time.time() * 1000)
        record = {
            "name": self.name,
            "attrs": dict(self.attrs),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "correlation_id": correlation_id.get(),
        }
        with _lock:
            _spans.append(record)
            if len(_spans) > _MAX:
                del _spans[: len(_spans) - _MAX]


@contextmanager
def span(name: str, **attrs: object) -> Iterator[Span]:
    """Record a timed span. Attrs must not include query, answer, body, or keys."""
    hidden = {"text", "query", "answer", "body", "template", "api_key", "authorization"}
    clean = {key: value for key, value in attrs.items() if key not in hidden}
    current = Span(name=name, attrs=clean, start_ms=int(time.time() * 1000))
    try:
        yield current
    finally:
        current.finish()


def drain_spans() -> list[dict]:
    with _lock:
        out = list(_spans)
        _spans.clear()
    return out
