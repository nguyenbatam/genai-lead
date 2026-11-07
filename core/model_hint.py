"""Constrain agent model hints. Gateway still performs the call."""

from __future__ import annotations

import random

from control.records import ModelCard


def unit_price(card: ModelCard) -> float:
    return float(card.input_price) + float(card.output_price)


def resolve_hint_model_id(
    *,
    hint: str | None,
    allowed_models: tuple[str, ...],
    prefer_cheap: bool,
    hint_calls: int,
    max_hint_calls: int,
    hint_cost_usd: float,
    max_hint_cost_usd: float,
    hint_canary_percent: int,
    default_card: ModelCard,
    candidates: dict[str, ModelCard],
    rng: random.Random | None = None,
) -> tuple[str | None, str]:
    """Return (model_id or None for default route, reason).

    reasons: default | applied | empty_allow | not_allowed | ceiling_calls |
    ceiling_cost | prefer_cheap | canary
    """
    if not allowed_models:
        return None, "empty_allow"
    if not hint:
        return None, "default"
    card = candidates.get(hint)
    if card is None:
        return None, "not_allowed"
    if card.id not in allowed_models and card.name not in allowed_models:
        return None, "not_allowed"
    if max_hint_calls >= 0 and hint_calls >= max_hint_calls:
        return None, "ceiling_calls"
    if max_hint_cost_usd > 0 and hint_cost_usd >= max_hint_cost_usd:
        return None, "ceiling_cost"
    percent = max(0, min(100, int(hint_canary_percent)))
    if percent < 100:
        roll = (rng or random.Random()).random() * 100
        if roll >= percent:
            return None, "canary"
    if prefer_cheap and unit_price(card) > unit_price(default_card):
        return None, "prefer_cheap"
    return card.id, "applied"


def index_models(cards: list[ModelCard]) -> dict[str, ModelCard]:
    """Map id and name → card for allow-list matching."""
    out: dict[str, ModelCard] = {}
    for card in cards:
        out[card.id] = card
        out[card.name] = card
    return out
