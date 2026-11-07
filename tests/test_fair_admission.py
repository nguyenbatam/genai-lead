from __future__ import annotations

import threading

import pytest

from control.errors import PlatformError
from control.fairness import fair_take
from control.records import AgentDef, Route
from control.store import new_id
from core.limiter import MemoryLimiter, RedisLimiter
from core.model import ModelOutput
from core.services import budget
from tests.conftest import card, project, publish, published_prompt
from tests.test_hardening import _route


def test_fair_take_round_robins_projects():
    items = [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2), ("c", 1)]
    picked = fair_take(items, lambda item: item[0], limit=3)
    assert [item[0] for item in picked] == ["a", "b", "c"]


def test_project_chat_concurrency_leaves_other_project_free(svc):
    noisy, _ = project(svc, name="noisy", limit=100)
    quiet, _ = project(svc, name="quiet", limit=100)
    _route(svc, noisy)
    _route(svc, quiet)
    gate = threading.Event()
    entered = threading.Semaphore(0)

    class Block:
        def complete(self, model, messages, tools=()):
            del model, messages, tools
            entered.release()
            assert gate.wait(2)
            return ModelOutput(text="ok", input_tokens=0, output_tokens=0)

    svc.gateway.model = Block()
    holders = [
        threading.Thread(target=lambda: svc.chat(noisy, "cheap", None, ["hi"])) for _ in range(2)
    ]
    for thread in holders:
        thread.start()
    for _ in range(2):
        assert entered.acquire(timeout=2)
    with pytest.raises(PlatformError) as caught:
        svc.chat(noisy, "cheap", None, ["more"])
    assert caught.value.status == 429
    assert "concurrency" in caught.value.detail
    svc.model.queue.append(ModelOutput(text="quiet-ok"))
    svc.gateway.model = svc.model
    assert svc.chat(quiet, "cheap", None, ["hi"]).text == "quiet-ok"
    gate.set()
    for thread in holders:
        thread.join(timeout=2)


def test_open_run_cap_stops_enqueue(svc):
    principal, _ = project(svc)
    stored = svc.store.get_project(principal.project_id)
    assert stored is not None
    stored.max_open_runs = 1
    prompt = published_prompt(svc, principal)
    created = card(svc, principal, "small", "cheap")
    agent = publish(
        svc,
        principal,
        AgentDef(
            id=new_id("agt"),
            project_id=principal.project_id,
            name="desk",
            version=1,
            status="draft",
            mode="agent",
            pattern="",
            prompt_id=prompt.id,
            model_profile="cheap",
            allowed_tools=(),
            members=(),
            entrypoint="agent",
            steps=(),
            budget=budget(),
        ),
    )
    svc.store.set_route(Route(principal.project_id, "cheap", created.id))
    first = svc.enqueue_run(principal, agent.id, "one")
    assert first.status == "queued"
    with pytest.raises(PlatformError) as caught:
        svc.enqueue_run(principal, agent.id, "two")
    assert caught.value.status == 429
    assert "open runs" in caught.value.detail


def test_ingest_fairness_serves_second_project(svc):
    noisy, _ = project(svc, name="noisy", limit=100)
    quiet, _ = project(svc, name="quiet", limit=100)
    noisy_kb = svc.create_kb(noisy, "n")
    quiet_kb = svc.create_kb(quiet, "q")
    for index in range(5):
        svc.submit_document(noisy, noisy_kb, f"n{index}", 1, f"body {index}", ("*",), f"n-{index}")
    svc.submit_document(quiet, quiet_kb, "q1", 1, "quiet body", ("*",), "q-1")
    seen: list[str] = []
    for _ in range(6):
        job = svc.work()
        assert job is not None
        seen.append(job.project_id)
    assert quiet.project_id in seen[:2]


def test_ingest_queue_cap(svc):
    principal, _ = project(svc)
    stored = svc.store.get_project(principal.project_id)
    assert stored is not None
    stored.max_queued_jobs = 1
    kb = svc.create_kb(principal, "docs")
    svc.submit_document(principal, kb, "a", 1, "one", ("*",), "a")
    with pytest.raises(PlatformError) as caught:
        svc.submit_document(principal, kb, "b", 1, "two", ("*",), "b")
    assert caught.value.status == 429
    assert "ingest queue" in caught.value.detail


def test_claim_due_fair_across_projects(svc):
    first, _ = project(svc, name="one", limit=100)
    second, _ = project(svc, name="two", limit=100)
    for principal in (first, second):
        prompt = published_prompt(svc, principal)
        card(svc, principal, "small", "cheap")
        agent = publish(
            svc,
            principal,
            AgentDef(
                id=new_id("agt"),
                project_id=principal.project_id,
                name="desk",
                version=1,
                status="draft",
                mode="agent",
                pattern="",
                prompt_id=prompt.id,
                model_profile="cheap",
                allowed_tools=(),
                members=(),
                entrypoint="agent",
                steps=(),
                budget=budget(),
            ),
        )
        for index in range(3):
            svc.enqueue_run(principal, agent.id, f"q{index}", f"{principal.project_id}-{index}")
    claimed = svc.store.claim_due(svc.clock.ms, 1_000, limit=2)
    projects = {run.project_id for run in claimed}
    assert projects == {first.project_id, second.project_id}


def test_memory_limiter_admits_up_to_limit():
    limiter = MemoryLimiter()
    assert limiter.acquire("p1", "chat", 2) is True
    assert limiter.acquire("p1", "chat", 2) is True
    assert limiter.acquire("p1", "chat", 2) is False
    assert limiter.acquire("p2", "chat", 2) is True
    limiter.release("p1", "chat")
    assert limiter.acquire("p1", "chat", 2) is True


def test_create_project_accepts_fair_admission_limits(svc):
    org = svc.create_org("acme")
    created = svc.create_project(
        org.id,
        "desk",
        100,
        max_in_flight_chat=3,
        max_in_flight_run=4,
        max_open_runs=10,
        max_queued_jobs=5,
        max_provider_inflight=1,
    )
    assert created.max_in_flight_chat == 3
    assert created.max_in_flight_run == 4
    assert created.max_open_runs == 10
    assert created.max_queued_jobs == 5
    assert created.max_provider_inflight == 1


def test_provider_concurrency_blocks_second_call(svc):
    principal, _ = project(svc)
    stored = svc.store.get_project(principal.project_id)
    assert stored is not None
    stored.max_provider_inflight = 1
    stored.max_in_flight_chat = 8
    _route(svc, principal)
    gate = threading.Event()
    entered = threading.Semaphore(0)

    class Block:
        def complete(self, model, messages, tools=()):
            del model, messages, tools
            entered.release()
            assert gate.wait(2)
            return ModelOutput(text="ok", input_tokens=0, output_tokens=0)

    svc.gateway.model = Block()
    holder = threading.Thread(target=lambda: svc.chat(principal, "cheap", None, ["hi"]))
    holder.start()
    assert entered.acquire(timeout=2)
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "cheap", None, ["more"])
    assert caught.value.status == 429
    assert "provider" in caught.value.detail
    gate.set()
    holder.join(timeout=2)


def test_worker_defers_when_run_slots_full(svc):
    principal, _ = project(svc)
    stored = svc.store.get_project(principal.project_id)
    assert stored is not None
    stored.max_in_flight_run = 1
    prompt = published_prompt(svc, principal)
    created = card(svc, principal, "small", "cheap")
    svc.store.set_route(Route(principal.project_id, "cheap", created.id))
    agent = publish(
        svc,
        principal,
        AgentDef(
            id=new_id("agt"),
            project_id=principal.project_id,
            name="desk",
            version=1,
            status="draft",
            mode="agent",
            pattern="",
            prompt_id=prompt.id,
            model_profile="cheap",
            allowed_tools=(),
            members=(),
            entrypoint="agent",
            steps=(),
            budget=budget(),
        ),
    )
    svc.model.queue.append(ModelOutput(text="done", finish=True, input_tokens=0, output_tokens=1))
    run = svc.enqueue_run(principal, agent.id, "hello")
    assert svc.limiter.acquire(principal.project_id, "run", 1) is True
    assert svc.sweep_runs() == 0
    assert svc.store.run_for(principal, run.id).status == "queued"
    svc.limiter.release(principal.project_id, "run")
    svc.clock.advance(1_000)
    assert svc.sweep_runs() == 1
    assert svc.store.run_for(principal, run.id).status == "completed"


def test_redis_limiter_lua_respects_limit():
    class Client:
        def __init__(self) -> None:
            self.values: dict[str, int] = {}

        def eval(self, script: str, numkeys: int, *args: object) -> int:
            del script, numkeys
            key = str(args[0])
            if len(args) >= 2 and str(args[1]).isdigit():
                limit = int(args[1])
                count = self.values.get(key, 0)
                if count >= limit:
                    return 0
                self.values[key] = count + 1
                return 1
            count = self.values.get(key, 0)
            if count <= 1:
                self.values.pop(key, None)
                return 0
            self.values[key] = count - 1
            return count - 1

        def ping(self) -> bool:
            return True

    limiter = RedisLimiter("redis://unused", client=Client())
    assert limiter.acquire("p1", "run", 1) is True
    assert limiter.acquire("p1", "run", 1) is False
    limiter.release("p1", "run")
    assert limiter.acquire("p1", "run", 1) is True
