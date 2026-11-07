from __future__ import annotations

import threading
import time

from control.records import AgentDef, ModelCard
from control.store import new_id
from core.model import ModelOutput, ToolCall
from core.services import budget, member
from core.tools import ToolRuntime
from tests.conftest import card, claims_of, project, publish, published_prompt, tool_call


def _route(svc, principal):
    created = card(svc, principal, "small", "cheap")
    svc.set_route(principal, "cheap", created.id, None)


def _agent(principal, prompt, **overrides) -> AgentDef:
    body = dict(
        id="agt_desk",
        project_id=principal.project_id,
        name="desk",
        version=1,
        status="draft",
        mode="agent",
        pattern="",
        prompt_id=prompt.id,
        model_profile="cheap",
        allowed_tools=("create_record",),
        members=(),
        entrypoint="",
        steps=(),
        budget=budget(tokens=10, handoffs=2),
    )
    body.update(overrides)
    return AgentDef(**body)


def test_engine_does_not_route_on_keywords(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _agent(principal, prompt, allowed_tools=()))
    svc.model.queue.append(
        ModelOutput(text="[NO_ANSWER]", finish=True, input_tokens=0, output_tokens=1)
    )
    run = svc.start_run(principal, agent.id, "tài liệu slack email hãy tìm")
    assert run.tool_calls == []
    assert run.outcome == "no_answer"


def test_ungranted_tool_is_denied(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _agent(principal, prompt, allowed_tools=()))
    svc.model.queue.append(tool_call("create_record", {"title": "x"}))
    run = svc.start_run(principal, agent.id, "make a record")
    assert run.outcome == "denied"
    assert svc.records == []


def test_approval_then_retry_does_not_apply_twice(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _agent(principal, prompt))
    svc.model.queue.append(tool_call("create_record", {"title": "ticket"}))
    waiting = svc.start_run(principal, agent.id, "open")
    assert waiting.outcome == "waiting_approval"
    assert svc.records == []
    queued = svc.approve(principal, waiting.id, waiting.pending_payload)
    assert queued.status == "queued"
    assert svc.records == []
    done = svc.resume(principal, queued.id)
    assert done.outcome == "no_answer"
    assert len(svc.records) == 1
    svc.model.queue.append(tool_call("create_record", {"title": "ticket"}))
    svc.engine.crash_after_steps = None
    again = svc.resume(principal, done.id)
    assert again.outcome == "no_answer"
    assert len(svc.records) == 1


def test_crash_resume_replays_the_write_from_the_ledger(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _agent(principal, prompt, allowed_tools=("note",)))
    svc.model.queue.extend(
        [
            tool_call("note", {"title": "once"}),
            tool_call("note", {"title": "once"}),
            ModelOutput(text="[NO_ANSWER]", finish=True, input_tokens=0, output_tokens=1),
        ]
    )
    svc.engine.crash_after_steps = 1
    try:
        svc.start_run(principal, agent.id, "note")
        raise AssertionError("crash expected")
    except RuntimeError:
        pass
    assert len(svc.tools.applied) == 1
    svc.engine.crash_after_steps = None
    run_id = next(iter(svc.store.runs))
    finished = svc.resume(principal, run_id)
    assert finished.outcome == "no_answer"
    assert len(svc.records) == 1


def test_handoff_does_not_reset_budget(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        _agent(
            principal,
            prompt,
            mode="supervisor",
            pattern="handoff",
            allowed_tools=(),
            members=(member("lead"), member("writer")),
            entrypoint="lead",
            budget=budget(tokens=5, handoffs=4),
        ),
    )
    svc.model.queue.append(ModelOutput(handoff="writer", input_tokens=0, output_tokens=5))
    run = svc.start_run(principal, agent.id, "pass it on")
    assert run.outcome == "failed"
    assert run.handoffs == 1
    assert run.tokens == 5
    assert svc.model.seen.count("pass it on") == 1
    assert "Answer from data." in svc.model.seen


def test_handoff_back_to_the_previous_agent_is_denied(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        _agent(
            principal,
            prompt,
            mode="supervisor",
            pattern="supervisor",
            members=(member("lead"), member("writer")),
            entrypoint="lead",
        ),
    )
    svc.model.queue.extend(
        [
            ModelOutput(handoff="writer", input_tokens=0, output_tokens=1),
            ModelOutput(handoff="lead", input_tokens=0, output_tokens=1),
        ]
    )
    run = svc.start_run(principal, agent.id, "bounce")
    assert run.outcome == "denied"


def test_parallel_fan_in_keeps_both_sides(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        _agent(
            principal,
            prompt,
            mode="supervisor",
            pattern="parallel",
            members=(member("lead"), member("researcher"), member("analyst")),
            entrypoint="lead",
        ),
    )
    svc.model.queue.extend(
        [
            ModelOutput(parallel=("researcher", "analyst"), input_tokens=0, output_tokens=1),
            ModelOutput(text="found", input_tokens=0, output_tokens=1),
            claims_of(("noted", "missing")),
        ]
    )
    run = svc.start_run(principal, agent.id, "split")
    assert run.outcome == "unverified"
    assert run.steps == 1
    assert run.tokens == 3


def test_hierarchical_pattern_is_rejected(svc):
    principal, _raw = project(svc)
    prompt = published_prompt(svc, principal)
    agent = _agent(
        principal,
        prompt,
        mode="supervisor",
        pattern="hierarchical",
        members=(member("lead"),),
        entrypoint="lead",
    )
    svc.create_agent(principal, agent)
    assert "hierarchical" in svc.validate_agent(principal, agent.id)


def test_restart_does_not_apply_the_write_again(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _agent(principal, prompt, allowed_tools=("note",)))
    svc.model.queue.extend(
        [
            tool_call("note", {"title": "once"}),
            tool_call("note", {"title": "once"}),
            ModelOutput(text="[NO_ANSWER]", finish=True, input_tokens=0, output_tokens=1),
        ]
    )
    svc.engine.crash_after_steps = 1
    try:
        svc.start_run(principal, agent.id, "note")
        raise AssertionError("crash expected")
    except RuntimeError:
        pass
    assert len(svc.records) == 1
    fresh = ToolRuntime(svc.store, svc._remember_record)
    fresh.specs = dict(svc.tools.specs)
    fresh.handlers = dict(svc.tools.handlers)
    svc.engine.tools = fresh
    svc.engine.crash_after_steps = None
    finished = svc.resume(principal, next(iter(svc.store.runs)))
    assert finished.outcome == "no_answer"
    assert len(svc.records) == 1
    assert fresh.applied == []


def test_second_tool_survives_approval(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        _agent(principal, prompt, allowed_tools=("create_record", "note")),
    )
    svc.model.queue.append(
        ModelOutput(
            tool_calls=(
                ToolCall("create_record", {"title": "a"}),
                ToolCall("note", {"title": "b"}),
            ),
            input_tokens=0,
            output_tokens=1,
        )
    )
    waiting = svc.start_run(principal, agent.id, "both")
    assert waiting.outcome == "waiting_approval"
    assert len(svc.records) == 0
    queued = svc.approve(principal, waiting.id, waiting.pending_payload)
    assert queued.status == "queued"
    done = svc.resume(principal, queued.id)
    assert done.outcome == "no_answer"
    assert len(svc.records) == 2


def test_parallel_crash_does_not_call_the_model_again(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        _agent(
            principal,
            prompt,
            mode="supervisor",
            pattern="parallel",
            members=(member("lead"), member("researcher"), member("analyst")),
            entrypoint="lead",
        ),
    )
    svc.model.queue.extend(
        [
            ModelOutput(parallel=("researcher", "analyst"), input_tokens=0, output_tokens=1),
            ModelOutput(text="found", input_tokens=0, output_tokens=1),
            claims_of(("noted", "missing")),
        ]
    )
    svc.engine.crash_after_model_calls = 2
    try:
        svc.start_run(principal, agent.id, "split")
        raise AssertionError("crash expected")
    except RuntimeError:
        pass
    assert len(svc.model.queue) == 1
    svc.engine.crash_after_model_calls = None
    finished = svc.resume(principal, next(iter(svc.store.runs)))
    assert finished.outcome == "unverified"
    assert finished.tokens == 3
    assert finished.steps == 1
    assert svc.model.queue == []


def test_enqueued_run_waits_for_the_worker(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _agent(principal, prompt, allowed_tools=()))
    svc.model.queue.append(
        ModelOutput(text="[NO_ANSWER]", finish=True, input_tokens=0, output_tokens=1)
    )
    run = svc.enqueue_run(principal, agent.id, "hello")
    assert run.status == "queued"
    assert svc.model.seen == []
    assert svc.sweep_runs() == 1
    done = svc.run(principal, run.id)
    assert done.outcome == "no_answer"


def test_approval_is_finished_by_the_worker(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _agent(principal, prompt))
    svc.model.queue.append(tool_call("create_record", {"title": "ticket"}))
    waiting = svc.start_run(principal, agent.id, "open")
    queued = svc.approve(principal, waiting.id, waiting.pending_payload)
    assert queued.status == "queued"
    assert svc.records == []
    assert svc.sweep_runs() == 1
    done = svc.run(principal, queued.id)
    assert done.outcome == "no_answer"
    assert len(svc.records) == 1


def test_parallel_fan_out_refuses_when_budget_cannot_cover_ceilings(svc):
    principal, _raw = project(svc)
    priced = svc.register_model(
        principal,
        ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name="pricey",
            profile="cheap",
            capabilities=("cheap",),
            input_price=1.0,
            output_price=1.0,
            pricing_version="2026-11",
        ),
    )
    svc.set_route(principal, "cheap", priced.id, None)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        _agent(
            principal,
            prompt,
            mode="supervisor",
            pattern="parallel",
            members=(member("lead"), member("researcher"), member("analyst")),
            entrypoint="lead",
            budget=budget(tokens=10_000, cost=0.001),
        ),
    )
    svc.model.queue.append(
        ModelOutput(parallel=("researcher", "analyst"), input_tokens=0, output_tokens=1)
    )
    run = svc.start_run(principal, agent.id, "split")
    assert run.outcome == "failed"
    assert run.failure == "budget"
    assert svc.model.queue == []


def test_parallel_members_overlap(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        _agent(
            principal,
            prompt,
            mode="supervisor",
            pattern="parallel",
            members=(member("lead"), member("researcher"), member("analyst")),
            entrypoint="lead",
        ),
    )

    class Fan:
        concurrent = True

        def __init__(self) -> None:
            self.calls = 0
            self.inside = 0
            self.overlap = 0
            self.lock = threading.Lock()

        def complete(self, model, messages, tools=()):
            del model, messages, tools
            with self.lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                return ModelOutput(
                    parallel=("researcher", "analyst"), input_tokens=0, output_tokens=1
                )
            with self.lock:
                self.inside += 1
                self.overlap = max(self.overlap, self.inside)
            time.sleep(0.05)
            with self.lock:
                self.inside -= 1
            if call == 2:
                return ModelOutput(text="found", input_tokens=0, output_tokens=1)
            return claims_of(("noted", "missing"))

    fan = Fan()
    svc.gateway.model = fan
    started = time.perf_counter()
    run = svc.start_run(principal, agent.id, "split")
    elapsed = time.perf_counter() - started
    assert run.outcome == "unverified"
    assert fan.overlap == 2
    assert elapsed < 0.09


def test_equal_success_prefers_the_cheaper_mode(svc):
    choice = svc.prefer(
        [
            {"name": "supervisor", "success": True, "cost": 0.2, "tokens": 30},
            {"name": "agent", "success": True, "cost": 0.05, "tokens": 10},
            {"name": "workflow", "success": True, "cost": 0.01, "tokens": 4},
        ]
    )
    assert choice == "workflow"
