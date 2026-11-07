"""Aggregate usage for cost decisions: free vs paid vs local per model."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from control.records import Usage
from control.vocab import USAGE_SUCCESS

# Cap scan so summary stays cheap on large ledgers.
SUMMARY_SCAN_LIMIT = 5_000


@dataclass
class TierStats:
    tier: str
    calls: int = 0
    success: int = 0
    failed: int = 0
    cost: float = 0.0
    tokens: int = 0
    latencies_ms: list[int] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        if self.calls == 0:
            return 0.0
        return self.failed / self.calls

    def as_dict(self) -> dict:
        ordered = sorted(self.latencies_ms)
        return {
            "tier": self.tier,
            "calls": self.calls,
            "success": self.success,
            "failed": self.failed,
            "cost": round(self.cost, 6),
            "tokens": self.tokens,
            "latency_p50_ms": _percentile_sorted(ordered, 50),
            "latency_p95_ms": _percentile_sorted(ordered, 95),
            "error_rate": round(self.error_rate, 4),
        }


@dataclass
class ModelStats:
    model_id: str
    model: str
    tiers: dict[str, TierStats] = field(default_factory=dict)

    @property
    def calls(self) -> int:
        return sum(item.calls for item in self.tiers.values())

    @property
    def cost(self) -> float:
        return sum(item.cost for item in self.tiers.values())

    @property
    def tokens(self) -> int:
        return sum(item.tokens for item in self.tiers.values())

    def as_dict(self) -> dict:
        tiers = [self.tiers[key].as_dict() for key in sorted(self.tiers)]
        return {
            "model_id": self.model_id,
            "model": self.model,
            "calls": self.calls,
            "cost": round(self.cost, 6),
            "tokens": self.tokens,
            "hint": cost_hint(self),
            "tiers": tiers,
        }


def summarize_usage(rows: list[Usage]) -> list[dict]:
    """Group by model, then tier. Empty tier becomes 'none'."""
    models: dict[str, ModelStats] = {}
    for row in rows:
        key = row.model_id or row.model_name
        stats = models.get(key)
        if stats is None:
            stats = ModelStats(model_id=row.model_id, model=row.model_name)
            models[key] = stats
        tier = row.tier or "none"
        bucket = stats.tiers.get(tier)
        if bucket is None:
            bucket = TierStats(tier=tier)
            stats.tiers[tier] = bucket
        bucket.calls += 1
        if row.status == USAGE_SUCCESS:
            bucket.success += 1
            bucket.cost += row.cost
        else:
            bucket.failed += 1
        bucket.tokens += row.input_tokens + row.output_tokens
        if row.latency_ms > 0:
            bucket.latencies_ms.append(row.latency_ms)
    return [models[key].as_dict() for key in sorted(models, key=lambda item: models[item].model)]


def cost_hint(stats: ModelStats) -> str:
    """Suggest tier_order from observed spend and reliability. Not a router."""
    paid = stats.tiers.get("paid")
    local = stats.tiers.get("local")
    free = stats.tiers.get("free")
    local_reliable = (
        local is not None and local.calls >= 3 and local.error_rate < 0.2 and local.success > 0
    )
    local_unreliable = local is not None and local.calls >= 3 and local.error_rate >= 0.5
    paid_costly = paid is not None and paid.calls >= 3 and paid.cost > 0
    paid_failing = paid is not None and paid.calls >= 3 and paid.error_rate >= 0.5
    only_free = (
        free is not None
        and free.calls >= 3
        and (paid is None or paid.calls == 0)
        and (local is None or local.calls == 0)
    )
    if local_unreliable:
        return "prefer_free_paid_local"
    if paid_failing and local_reliable:
        return "prefer_free_local_paid"
    if paid_costly and local_reliable:
        return "prefer_free_local_paid"
    if only_free:
        return "keep_free"
    if paid_costly and (local is None or local.calls == 0):
        return "consider_local"
    return "observe"


def _percentile_sorted(ordered: list[int], pct: float) -> int:
    if not ordered:
        return 0
    rank = max(1, math.ceil(pct / 100 * len(ordered))) - 1
    return ordered[min(rank, len(ordered) - 1)]
