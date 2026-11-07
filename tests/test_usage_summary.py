from __future__ import annotations

from control.records import Usage
from control.store import new_id
from core.model import ModelOutput
from core.usage_summary import ModelStats, TierStats, cost_hint, summarize_usage
from tests.conftest import card, project


def test_summarize_groups_by_model_and_tier():
    model = new_id("mdl")
    rows = [
        Usage(
            project_id="p",
            model_id=model,
            model_name="llama",
            pricing_version="v",
            input_tokens=2,
            output_tokens=3,
            cost=0.0,
            status="success",
            tier="free",
            latency_ms=10,
        ),
        Usage(
            project_id="p",
            model_id=model,
            model_name="llama",
            pricing_version="v",
            input_tokens=2,
            output_tokens=3,
            cost=0.05,
            status="success",
            tier="paid",
            latency_ms=40,
        ),
        Usage(
            project_id="p",
            model_id=model,
            model_name="llama",
            pricing_version="v",
            input_tokens=0,
            output_tokens=0,
            cost=0.0,
            status="failed",
            tier="paid",
            latency_ms=0,
        ),
    ]
    summary = summarize_usage(rows)
    assert len(summary) == 1
    assert summary[0]["model"] == "llama"
    assert summary[0]["calls"] == 3
    assert summary[0]["cost"] == 0.05
    tiers = {item["tier"]: item for item in summary[0]["tiers"]}
    assert tiers["free"]["calls"] == 1
    assert tiers["paid"]["failed"] == 1
    assert tiers["paid"]["error_rate"] == 0.5


def test_hint_prefers_local_when_paid_costs_and_local_is_reliable():
    stats = ModelStats(model_id="m", model="x")
    stats.tiers["paid"] = TierStats(tier="paid", calls=5, success=5, failed=0, cost=1.2)
    stats.tiers["local"] = TierStats(tier="local", calls=5, success=5, failed=0, cost=0.0)
    assert cost_hint(stats) == "prefer_free_local_paid"


def test_hint_unreliable_local():
    stats = ModelStats(model_id="m", model="x")
    stats.tiers["local"] = TierStats(tier="local", calls=4, success=1, failed=3)
    stats.tiers["paid"] = TierStats(tier="paid", calls=4, success=4, failed=0, cost=1.0)
    assert cost_hint(stats) == "prefer_free_paid_local"


def test_hint_consider_local_when_paid_only():
    stats = ModelStats(model_id="m", model="x")
    stats.tiers["paid"] = TierStats(tier="paid", calls=5, success=5, failed=0, cost=2.0)
    assert cost_hint(stats) == "consider_local"


def test_hint_keep_free_and_observe():
    free_only = ModelStats(model_id="m", model="x")
    free_only.tiers["free"] = TierStats(tier="free", calls=4, success=4, failed=0)
    assert cost_hint(free_only) == "keep_free"
    empty = ModelStats(model_id="m", model="x")
    assert cost_hint(empty) == "observe"


def test_hint_paid_failing_prefers_local():
    stats = ModelStats(model_id="m", model="x")
    stats.tiers["paid"] = TierStats(tier="paid", calls=4, success=1, failed=3, cost=0.5)
    stats.tiers["local"] = TierStats(tier="local", calls=4, success=4, failed=0)
    assert cost_hint(stats) == "prefer_free_local_paid"


def test_usage_summary_endpoint_and_metrics_labels(svc):
    principal, _ = project(svc)
    created = card(svc, principal, "priced", "cheap")
    # price the card via a second registration path: mutate store card
    priced = svc.store.models[created.id]
    priced.input_price = 1.0
    priced.output_price = 1.0
    svc.set_route(principal, "cheap", created.id, None)
    svc.model.queue.append(ModelOutput(text="ok", finish=True, input_tokens=2, output_tokens=3))
    svc.chat(principal, "cheap", None, ["q"])
    body = svc.usage_summary(principal)
    assert body["scanned"] == 1
    assert body["models"][0]["model"] == "priced"
    assert body["models"][0]["tiers"][0]["tier"] == "none"
    text = svc.metrics.render()
    assert 'genai_provider_attempts_total{model="priced",outcome="ok",tier="none"}' in text
    assert "genai_provider_cost_micros_total" in text


def test_usage_summary_scans_newest_rows(svc):
    principal, _ = project(svc)
    old = card(svc, principal, "old-model", "cheap")
    fresh = card(svc, principal, "fresh-model", "cheap")
    for _ in range(5):
        svc.store.record_usage(
            Usage(
                project_id=principal.project_id,
                model_id=old.id,
                model_name="old-model",
                pricing_version="v",
                input_tokens=1,
                output_tokens=1,
                cost=0.0,
                status="success",
                tier="free",
            )
        )
    svc.store.record_usage(
        Usage(
            project_id=principal.project_id,
            model_id=fresh.id,
            model_name="fresh-model",
            pricing_version="v",
            input_tokens=1,
            output_tokens=1,
            cost=0.0,
            status="success",
            tier="paid",
        )
    )
    body = svc.usage_summary(principal, limit=2)
    assert body["scanned"] == 2
    assert {item["model"] for item in body["models"]} == {"old-model", "fresh-model"}
    only_latest = svc.usage_summary(principal, limit=1)
    assert only_latest["models"][0]["model"] == "fresh-model"
