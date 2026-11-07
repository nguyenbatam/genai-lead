"""Allow-listed model hints with prefer-cheap and ceilings."""

from __future__ import annotations

import random

from control.records import AgentDef, ModelCard
from control.store import new_id
from core.model import ModelOutput, ToolCall
from core.model_hint import index_models, resolve_hint_model_id, unit_price
from tests.conftest import budget, project, publish, published_prompt


def _card(project_id: str, name: str, *, inp: float, out: float) -> ModelCard:
    return ModelCard(
        id=new_id("mdl"),
        project_id=project_id,
        name=name,
        profile="cheap",
        capabilities=("cheap",),
        input_price=inp,
        output_price=out,
        pricing_version="2026-11",
    )


def test_resolve_prefer_cheap_blocks_expensive():
    cheap = _card("p", "cheap-model", inp=0.1, out=0.1)
    dear = _card("p", "dear-model", inp=5.0, out=5.0)
    candidates = index_models([cheap, dear])
    model_id, reason = resolve_hint_model_id(
        hint=dear.name,
        allowed_models=(cheap.name, dear.name),
        prefer_cheap=True,
        hint_calls=0,
        max_hint_calls=2,
        hint_cost_usd=0.0,
        max_hint_cost_usd=0.0,
        hint_canary_percent=100,
        default_card=cheap,
        candidates=candidates,
    )
    assert model_id is None
    assert reason == "prefer_cheap"
    assert unit_price(dear) > unit_price(cheap)


def test_resolve_applies_when_prefer_cheap_off():
    cheap = _card("p", "cheap-model", inp=0.1, out=0.1)
    dear = _card("p", "dear-model", inp=5.0, out=5.0)
    model_id, reason = resolve_hint_model_id(
        hint=dear.id,
        allowed_models=(cheap.name, dear.id),
        prefer_cheap=False,
        hint_calls=0,
        max_hint_calls=2,
        hint_cost_usd=0.0,
        max_hint_cost_usd=0.0,
        hint_canary_percent=100,
        default_card=cheap,
        candidates=index_models([cheap, dear]),
    )
    assert model_id == dear.id
    assert reason == "applied"


def test_resolve_ceiling_calls_and_cost_and_canary():
    cheap = _card("p", "c", inp=0.1, out=0.1)
    dear = _card("p", "d", inp=0.1, out=0.1)
    candidates = index_models([cheap, dear])
    _, reason = resolve_hint_model_id(
        hint=dear.name,
        allowed_models=(dear.name,),
        prefer_cheap=False,
        hint_calls=1,
        max_hint_calls=1,
        hint_cost_usd=0.0,
        max_hint_cost_usd=0.0,
        hint_canary_percent=100,
        default_card=cheap,
        candidates=candidates,
    )
    assert reason == "ceiling_calls"
    _, reason = resolve_hint_model_id(
        hint=dear.name,
        allowed_models=(dear.name,),
        prefer_cheap=False,
        hint_calls=0,
        max_hint_calls=3,
        hint_cost_usd=1.0,
        max_hint_cost_usd=0.5,
        hint_canary_percent=100,
        default_card=cheap,
        candidates=candidates,
    )
    assert reason == "ceiling_cost"
    _, reason = resolve_hint_model_id(
        hint=dear.name,
        allowed_models=(dear.name,),
        prefer_cheap=False,
        hint_calls=0,
        max_hint_calls=3,
        hint_cost_usd=0.0,
        max_hint_cost_usd=0.0,
        hint_canary_percent=0,
        default_card=cheap,
        candidates=candidates,
        rng=random.Random(0),
    )
    assert reason == "canary"


def test_engine_applies_hint_next_turn(svc):
    principal, _ = project(svc)
    cheap = svc.register_model(
        principal,
        _card(principal.project_id, "hint-cheap", inp=0.0, out=0.0),
    )
    dear = svc.register_model(
        principal,
        _card(principal.project_id, "hint-dear", inp=0.0, out=0.0),
    )
    svc.set_route(principal, "cheap", cheap.id, None)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        AgentDef(
            id=new_id("agt"),
            project_id=principal.project_id,
            name="hinted2",
            version=1,
            status="draft",
            mode="agent",
            pattern="",
            prompt_id=prompt.id,
            model_profile="cheap",
            allowed_tools=("note",),
            members=(),
            entrypoint="",
            steps=(),
            budget=budget(),
            allowed_models=(cheap.name, dear.name),
            prefer_cheap=False,
            max_hint_calls=2,
        ),
    )
    svc.model.queue.extend(
        [
            ModelOutput(
                tool_calls=(ToolCall("note", {"t": "1"}),),
                model_hint=dear.name,
                input_tokens=1,
                output_tokens=1,
            ),
            ModelOutput(text="done", finish=True, input_tokens=1, output_tokens=1),
        ]
    )
    done = svc.start_run(principal, agent.id, "note please")
    assert done.status == "completed"
    assert done.hint_calls == 1
    assert any(
        event.get("name") == "model_hint" and "applied" in (event.get("hits") or [])
        for event in done.events
    )


def test_engine_prefer_cheap_skips_dear_hint(svc):
    principal, _ = project(svc)
    cheap = svc.register_model(
        principal,
        _card(principal.project_id, "pc-cheap", inp=0.1, out=0.1),
    )
    dear = svc.register_model(
        principal,
        _card(principal.project_id, "pc-dear", inp=9.0, out=9.0),
    )
    svc.set_route(principal, "cheap", cheap.id, None)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        AgentDef(
            id=new_id("agt"),
            project_id=principal.project_id,
            name="prefer",
            version=1,
            status="draft",
            mode="agent",
            pattern="",
            prompt_id=prompt.id,
            model_profile="cheap",
            allowed_tools=("note",),
            members=(),
            entrypoint="",
            steps=(),
            budget=budget(),
            allowed_models=(cheap.name, dear.name),
            prefer_cheap=True,
            max_hint_calls=2,
        ),
    )
    svc.model.queue.extend(
        [
            ModelOutput(
                tool_calls=(ToolCall("note", {"t": "1"}),),
                model_hint=dear.name,
                input_tokens=1,
                output_tokens=1,
            ),
            ModelOutput(text="done", finish=True, input_tokens=1, output_tokens=1),
        ]
    )
    done = svc.start_run(principal, agent.id, "note please")
    assert done.status == "completed"
    assert done.hint_calls == 0
    assert any(
        event.get("name") == "model_hint" and "prefer_cheap" in (event.get("hits") or [])
        for event in done.events
    )
