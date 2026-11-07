"""Named vocab sets stay aligned with the product contract."""

from control.vocab import (
    CREDENTIAL_TIERS,
    EVAL_STATUSES,
    JOB_STATUSES,
    JOB_VERSION_HELD,
    MODES,
    OPEN_RUN,
    OUTCOMES,
    ROLES,
    RUN_STATUSES,
    TERMINAL,
    TIER_AFFINITIES,
    TIER_ORDERS,
)


def test_run_status_partition() -> None:
    assert OPEN_RUN.isdisjoint(TERMINAL)
    assert RUN_STATUSES == OPEN_RUN | TERMINAL


def test_outcomes_match_agents_contract() -> None:
    assert OUTCOMES == frozenset(
        {
            "completed",
            "no_answer",
            "ungrounded",
            "unverified",
            "waiting_approval",
            "refused",
            "denied",
            "failed",
            "cancelled",
        }
    )


def test_closed_sets_nonempty() -> None:
    assert ROLES and MODES and JOB_STATUSES and EVAL_STATUSES and TIER_AFFINITIES
    assert TIER_ORDERS == frozenset({"free_paid_local", "free_local_paid"})
    assert CREDENTIAL_TIERS == frozenset({"free", "paid", "local"})
    assert JOB_VERSION_HELD <= JOB_STATUSES
