from __future__ import annotations

import pytest

from control.errors import PlatformError
from control.records import ModelCard
from control.store import new_id
from core.engine import LEASE_MS
from core.gateway import Breaker
from core.model import PROVIDER_TIMEOUT_S, ModelOutput, ProviderRejected, tool_calls_from
from tests.conftest import card, project


def test_fallback_runs_only_when_capability_matches(svc):
    principal, _raw = project(svc)
    primary = card(svc, principal, "big", "reason")
    fallback = card(svc, principal, "big-standby", "reason")
    cheap = card(svc, principal, "small", "cheap")
    svc.set_route(principal, "reason", primary.id, fallback.id)
    svc.model.down.add(primary.id)
    svc.model.queue.append(ModelOutput(text="from fallback", input_tokens=2, output_tokens=3))
    output = svc.chat(principal, "reason", None, ["question"])
    assert output.text == "from fallback"
    rows = svc.usage(principal)
    assert [row.status for row in rows] == ["failed", "success"]
    assert rows[1].fallback_from == primary.id
    assert rows[1].pricing_version == "2026-11"
    assert rows[1].cost == 0
    svc.set_route(principal, "reason", primary.id, cheap.id)
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "reason", None, ["again"])
    assert caught.value.status == 502


def test_breaker_stops_calling_a_dead_model(svc):
    principal, _raw = project(svc)
    primary = card(svc, principal, "big", "reason")
    svc.set_route(principal, "reason", primary.id, None)
    svc.model.down.add(primary.id)
    for _ in range(3):
        with pytest.raises(PlatformError):
            svc.chat(principal, "reason", None, ["question"])
    seen = len(svc.model.seen)
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "reason", None, ["question"])
    assert caught.value.status == 502
    assert len(svc.model.seen) == seen


def test_client_error_does_not_fall_back(svc):
    class Rejecting:
        def complete(self, model, messages, tools=()):
            del messages, tools
            raise ProviderRejected(model.id)

    principal, _raw = project(svc)
    primary = card(svc, principal, "big", "reason")
    fallback = card(svc, principal, "standby", "reason")
    svc.set_route(principal, "reason", primary.id, fallback.id)
    svc.gateway.model = Rejecting()
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "reason", None, ["question"])
    assert caught.value.status == 400
    assert svc.usage(principal) == []


def test_half_open_allows_one_probe():
    breaker = Breaker(limit=1, cooldown_s=10)
    now = 100.0
    breaker.failure("m", now)
    assert breaker.allow("m", now + 1) is False
    assert breaker.allow("m", now + 11) is True
    assert breaker.allow("m", now + 11) is False
    breaker.success("m")
    assert breaker.allow("m", now + 12) is True


def test_bad_tool_json_is_rejected():
    with pytest.raises(ProviderRejected):
        tool_calls_from("mdl", {"tool_calls": [{"function": {"name": "note", "arguments": "{"}}]})


def test_lease_outlasts_one_provider_call():
    assert LEASE_MS >= PROVIDER_TIMEOUT_S * 2 * 1000


def test_open_breaker_uses_the_declared_fallback(svc):
    principal, _raw = project(svc)
    primary = card(svc, principal, "big", "reason")
    fallback = card(svc, principal, "standby", "reason")
    svc.set_route(principal, "reason", primary.id, fallback.id)
    svc.model.down.add(primary.id)
    for _ in range(3):
        svc.chat(principal, "reason", None, ["question"])
    svc.model.queue.append(ModelOutput(text="saved", input_tokens=1, output_tokens=1))
    before = len(svc.model.seen)
    output = svc.chat(principal, "reason", None, ["question"])
    assert output.text == "saved"
    assert len(svc.model.seen) - before == 1
    success = [row for row in svc.usage(principal) if row.status == "success"][-1]
    assert success.model_id == fallback.id
    assert success.fallback_from == primary.id
    assert 'genai_provider_fallbacks_total{model="standby",tier="none"}' in svc.metrics.render()


def test_fallback_chain_reaches_selfhost(svc):
    principal, _raw = project(svc)
    primary = card(svc, principal, "cloud", "reason")
    paid = card(svc, principal, "cloud-paid", "reason")
    local = svc.register_model(
        principal,
        ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name="local-llama",
            profile="reason",
            capabilities=("reason",),
            input_price=0.0,
            output_price=0.0,
            pricing_version="2026-11",
            provider="selfhost",
        ),
    )
    svc.set_route(
        principal,
        "reason",
        primary.id,
        fallback_model_ids=[paid.id, local.id],
    )
    svc.model.down.add(primary.id)
    svc.model.down.add(paid.id)
    svc.model.queue.append(ModelOutput(text="from local", input_tokens=1, output_tokens=1))
    output = svc.chat(principal, "reason", None, ["question"])
    assert output.text == "from local"
    rows = svc.usage(principal)
    assert [row.status for row in rows] == ["failed", "failed", "success"]
    assert rows[2].model_id == local.id
    assert rows[2].fallback_from == paid.id
    assert "genai_provider_fallbacks_total" in svc.metrics.render()


def test_chain_skips_capability_mismatch(svc):
    principal, _raw = project(svc)
    primary = card(svc, principal, "big", "reason")
    cheap = card(svc, principal, "small", "cheap")
    standby = card(svc, principal, "standby", "reason")
    svc.set_route(
        principal,
        "reason",
        primary.id,
        fallback_model_ids=[cheap.id, standby.id],
    )
    svc.model.down.add(primary.id)
    svc.model.queue.append(ModelOutput(text="standby", input_tokens=1, output_tokens=1))
    output = svc.chat(principal, "reason", None, ["question"])
    assert output.text == "standby"
    assert [row.model_id for row in svc.usage(principal)] == [primary.id, standby.id]


def test_http_retries_metric_labeled_by_model_and_tier(svc):
    class Retrying:
        last_http_retries = 2
        last_wire_ms = 12

        def complete(self, model, messages, tools=(), **_kwargs):
            del model, messages, tools
            return ModelOutput(text="ok", finish=True, input_tokens=1, output_tokens=1)

    principal, _raw = project(svc)
    primary = card(svc, principal, "wired", "cheap")
    svc.set_route(principal, "cheap", primary.id, None)
    svc.gateway.model = Retrying()
    svc.chat(principal, "cheap", None, ["q"])
    text = svc.metrics.render()
    assert 'genai_provider_retries_total{model="wired",tier="none"} 2' in text
    rows = svc.usage(principal)
    assert rows[0].latency_ms == 12
