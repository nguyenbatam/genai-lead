"""In-process histograms and counters. One replica, one scrape target. No extra library."""

from __future__ import annotations

import threading

_BOUNDS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10_000, 30_000, 60_000)


class Histogram:
    def __init__(self) -> None:
        self.bounds = _BOUNDS
        self.counts = [0 for _ in self.bounds]
        self.inf = 0
        self.total = 0
        self.sum_ms = 0
        self._lock = threading.Lock()

    def observe(self, ms: int) -> None:
        with self._lock:
            self.total += 1
            self.sum_ms += ms
            for index, bound in enumerate(self.bounds):
                if ms <= bound:
                    self.counts[index] += 1
                    return
            self.inf += 1

    def render(self, name: str) -> str:
        with self._lock:
            lines: list[str] = []
            running = 0
            for bound, count in zip(self.bounds, self.counts, strict=True):
                running += count
                lines.append(f'{name}_bucket{{le="{bound}"}} {running}')
            lines.append(f'{name}_bucket{{le="+Inf"}} {running + self.inf}')
            lines.append(f"{name}_sum {self.sum_ms}")
            lines.append(f"{name}_count {self.total}")
            return "\n".join(lines)


class Counter:
    """Labeled counter. Label sets stay small (tier, outcome)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[tuple[str, str], ...], int] = {}

    def inc(self, amount: int = 1, **labels: str) -> None:
        key = tuple(sorted((str(name), str(value)) for name, value in labels.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def render(self, name: str) -> str:
        with self._lock:
            if not self._values:
                return f"{name} 0"
            lines: list[str] = []
            for key, value in sorted(self._values.items()):
                if not key:
                    lines.append(f"{name} {value}")
                    continue
                joined = ",".join(f'{label}="{val}"' for label, val in key)
                lines.append(f"{name}{{{joined}}} {value}")
            return "\n".join(lines)


class Metrics:
    def __init__(self) -> None:
        self.requests = Histogram()
        self.provider = Histogram()
        self.attempts = Counter()
        self.retries = Counter()
        self.fallbacks = Counter()
        self.cost_micros = Counter()

    def render(self) -> str:
        return "\n".join(
            (
                "# TYPE genai_request_ms histogram",
                self.requests.render("genai_request_ms"),
                "# TYPE genai_provider_ms histogram",
                self.provider.render("genai_provider_ms"),
                "# TYPE genai_provider_attempts_total counter",
                self.attempts.render("genai_provider_attempts_total"),
                "# TYPE genai_provider_retries_total counter",
                self.retries.render("genai_provider_retries_total"),
                "# TYPE genai_provider_fallbacks_total counter",
                self.fallbacks.render("genai_provider_fallbacks_total"),
                "# TYPE genai_provider_cost_micros_total counter",
                self.cost_micros.render("genai_provider_cost_micros_total"),
            )
        )
