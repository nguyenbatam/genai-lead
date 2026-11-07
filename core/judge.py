"""A claim is grounded only when its text sits inside the cited passage."""

from __future__ import annotations

from control.vocab import (
    OUTCOME_COMPLETED,
    OUTCOME_NO_ANSWER,
    OUTCOME_UNGROUNDED,
    OUTCOME_UNVERIFIED,
)
from core.model import ModelOutput


def judge(output: ModelOutput, passages: dict[str, str]) -> str:
    if output.text.strip() == "[NO_ANSWER]":
        return OUTCOME_NO_ANSWER
    if not output.claims:
        return OUTCOME_UNGROUNDED
    supported = False
    for claim in output.claims:
        passage = passages.get(claim.cite)
        if passage is None:
            continue
        if _norm(claim.text) not in _norm(passage):
            return OUTCOME_UNGROUNDED
        supported = True
    if not supported:
        return OUTCOME_UNVERIFIED
    return OUTCOME_COMPLETED


def _norm(text: str) -> str:
    return " ".join(text.split())
