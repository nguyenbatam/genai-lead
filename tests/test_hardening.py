from __future__ import annotations

import threading

import pytest

from control.errors import PlatformError
from control.records import AgentDef, ModelCard
from control.store import new_id
from core.model import ModelOutput, ProviderRejected
from core.quota import RedisQuota
from core.services import budget, member
from tests.conftest import card, project, publish, published_prompt, tool_call, workflow


def _route(svc, principal, price: float = 0.0):
    created = svc.register_model(
        principal,
        ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name="small",
            profile="cheap",
            capabilities=("cheap",),
            input_price=price,
            output_price=price,
            pricing_version="2026-11",
        ),
    )
    svc.set_route(principal, "cheap", created.id, None)
    return created


def test_request_limit_must_be_positive(svc):
    with pytest.raises(PlatformError) as caught:
        project(svc, limit=0)
    assert caught.value.status == 400


def test_quota_admits_each_request_once(svc):
    principal, _raw = project(svc, limit=5)
    results: list[int] = []

    def hit() -> None:
        try:
            svc._quota(principal)
        except PlatformError:
            results.append(0)
            return
        results.append(1)

    threads = [threading.Thread(target=hit) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(1) == 5


def test_explicit_model_does_not_use_another_route_fallback(svc):
    principal, _raw = project(svc)
    primary = card(svc, principal, "big", "reason")
    fallback = card(svc, principal, "standby", "reason")
    other = card(svc, principal, "other", "reason")
    svc.set_route(principal, "reason", primary.id, fallback.id)
    svc.model.down.add(other.id)
    svc.model.queue.append(ModelOutput(text="should not run"))
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "reason", other.id, ["question"])
    assert caught.value.status == 502
    assert [row.status for row in svc.usage(principal)] == ["failed"]


def test_fallback_must_cover_primary_capabilities(svc):
    principal, _raw = project(svc)
    primary = svc.register_model(
        principal,
        ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name="wide",
            profile="chat",
            capabilities=("chat", "embed"),
            input_price=1,
            output_price=1,
            pricing_version="2026-11",
        ),
    )
    narrow = svc.register_model(
        principal,
        ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name="embed-only",
            profile="embed",
            capabilities=("embed",),
            input_price=1,
            output_price=1,
            pricing_version="2026-11",
        ),
    )
    svc.set_route(principal, "chat", primary.id, narrow.id)
    svc.model.down.add(primary.id)
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "chat", None, ["question"])
    assert caught.value.status == 502


def test_cost_uses_integer_micros(svc):
    principal, _raw = project(svc)
    created = card(svc, principal, "priced", "cheap")
    created.input_price = 2.0
    created.output_price = 4.0
    svc.set_route(principal, "cheap", created.id, None)
    svc.model.queue.append(ModelOutput(text="ok", input_tokens=3, output_tokens=1))
    svc.chat(principal, "cheap", None, ["hi"])
    assert round(svc.usage(principal)[0].cost * 1_000_000) == 10


def test_run_stops_when_the_call_exceeds_budget(svc):
    principal, _raw = project(svc)
    _route(svc, principal, price=2.0)
    prompt = published_prompt(svc, principal)
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
            entrypoint="",
            steps=(),
            budget=budget(cost=0.000001),
        ),
    )
    svc.model.queue.append(ModelOutput(text="done", finish=True, input_tokens=0, output_tokens=1))
    run = svc.start_run(principal, agent.id, "go")
    assert run.outcome == "failed"
    assert run.failure == "budget"


def test_viewer_cannot_approve(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
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
            allowed_tools=("create_record",),
            members=(),
            entrypoint="",
            steps=(),
            budget=budget(),
        ),
    )
    svc.model.queue.append(tool_call("create_record", {"title": "ticket"}))
    waiting = svc.start_run(principal, agent.id, "open")
    viewer = svc.store.authenticate(svc.issue_key(principal.project_id, "viewer", ("*",)))
    assert viewer is not None
    with pytest.raises(PlatformError) as caught:
        svc.approve(viewer, waiting.id, waiting.pending_payload)
    assert caught.value.status == 403
    assert svc.records == []


def test_same_run_key_replays_and_rejects_a_different_query(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
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
            entrypoint="",
            steps=(),
            budget=budget(),
        ),
    )
    svc.model.queue.append(ModelOutput(text="[NO_ANSWER]", finish=True))
    first = svc.start_run(principal, agent.id, "hello", "run-1")
    second = svc.start_run(principal, agent.id, "hello", "run-1")
    assert first.id == second.id
    with pytest.raises(PlatformError) as caught:
        svc.start_run(principal, agent.id, "other", "run-1")
    assert caught.value.status == 409


def test_expired_lease_is_resumed(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
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
            allowed_tools=("note",),
            members=(),
            entrypoint="",
            steps=(),
            budget=budget(),
        ),
    )
    svc.model.queue.extend(
        [
            tool_call("note", {"title": "once"}),
            tool_call("note", {"title": "once"}),
            ModelOutput(text="[NO_ANSWER]", finish=True, input_tokens=0, output_tokens=1),
        ]
    )
    svc.engine.crash_after_steps = 1
    with pytest.raises(RuntimeError):
        svc.start_run(principal, agent.id, "note")
    svc.engine.crash_after_steps = None
    assert svc.sweep_runs() == 1
    finished = svc.run(principal, next(iter(svc.store.runs)))
    assert finished.outcome == "no_answer"
    assert len(svc.records) == 1


def test_waiting_run_expires(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
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
            allowed_tools=("create_record",),
            members=(),
            entrypoint="",
            steps=(),
            budget=budget(wall=1_000),
        ),
    )
    svc.model.queue.append(tool_call("create_record", {"title": "ticket"}))
    waiting = svc.start_run(principal, agent.id, "open")
    assert waiting.outcome == "waiting_approval"
    svc.clock.advance(1_001)
    assert svc.sweep_runs() == 1
    expired = svc.run(principal, waiting.id)
    assert expired.failure == "wall"


def test_member_turn_cap(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(
        svc,
        principal,
        AgentDef(
            id=new_id("agt"),
            project_id=principal.project_id,
            name="team",
            version=1,
            status="draft",
            mode="supervisor",
            pattern="parallel",
            prompt_id=prompt.id,
            model_profile="cheap",
            allowed_tools=(),
            members=(member("lead"), member("researcher", turns=1)),
            entrypoint="lead",
            steps=(),
            budget=budget(),
        ),
    )
    svc.model.queue.extend(
        [
            ModelOutput(parallel=("researcher", "researcher"), input_tokens=0, output_tokens=1),
            ModelOutput(text="found", input_tokens=0, output_tokens=1),
        ]
    )
    run = svc.start_run(principal, agent.id, "split")
    assert run.failure == "turns"


def test_foreign_knowledge_base_is_denied(svc):
    left, _raw = project(svc, "left")
    right, _raw = project(svc, "right")
    _route(svc, right)
    kb = svc.create_kb(left, "policies")
    prompt = published_prompt(svc, right)
    agent = publish(svc, right, workflow(right, prompt, kb))
    svc.model.queue.append(ModelOutput(text="[NO_ANSWER]", finish=True))
    run = svc.start_run(right, agent.id, "secret")
    assert run.outcome == "denied"


def test_older_document_version_is_rejected(svc):
    principal, _raw = project(svc)
    kb = svc.create_kb(principal, "policies")
    svc.submit_document(principal, kb, "doc", 1, "one", ("*",), "v1")
    assert svc.work().status == "indexed"
    with pytest.raises(PlatformError) as caught:
        svc.submit_document(principal, kb, "doc", 1, "again", ("*",), "v1b")
    assert caught.value.status == 409


def test_idempotency_key_rejects_a_different_body(svc):
    principal, _raw = project(svc)
    kb = svc.create_kb(principal, "policies")
    svc.submit_document(principal, kb, "doc", 1, "one", ("*",), "same")
    with pytest.raises(PlatformError) as caught:
        svc.submit_document(principal, kb, "doc", 1, "two", ("*",), "same")
    assert caught.value.status == 409


def test_stale_holder_cannot_overwrite_a_run(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, workflow(principal, prompt, ""))
    svc.model.queue.append(ModelOutput(text="[NO_ANSWER]", finish=True))
    run = svc.start_run(principal, agent.id, "hello")
    held = svc.store.lease_of(run.id)
    assert svc.store.renew_lease(run.id, held, held + 5_000)
    run.answer = "stale"
    assert svc.store.save_run(run, held) is False
    assert svc.run(principal, run.id).answer != "stale"


def test_chat_redacts_before_the_provider(svc):
    principal, raw = project(svc)
    created = _route(svc, principal)
    del created
    svc.model.queue.append(ModelOutput(text="ok"))
    svc.chat(principal, "cheap", None, [f"account 123456789012 key {raw}"])
    seen = " ".join(svc.model.seen)
    assert "123456789012" not in seen
    assert raw not in seen


def test_quota_window_admits_again(svc):
    principal, _raw = project(svc, limit=1)
    _route(svc, principal)
    svc.model.queue.append(ModelOutput(text="ok"))
    svc.chat(principal, "cheap", None, ["hi"])
    svc.clock.advance(60_000)
    svc.model.queue.append(ModelOutput(text="ok"))
    assert svc.chat(principal, "cheap", None, ["later"]).text == "ok"


def test_viewer_cannot_call_the_model(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    viewer = svc.store.authenticate(svc.issue_key(principal.project_id, "viewer", ("*",)))
    assert viewer is not None
    with pytest.raises(PlatformError) as caught:
        svc.chat(viewer, "cheap", None, ["hi"])
    assert caught.value.status == 403
    assert svc.model.seen == []


def test_async_chat_queues_and_worker_completes(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    svc.model.queue.append(ModelOutput(text="later", finish=True))
    parked = svc.chat_async(principal, "cheap", None, ["hi"], "async-1")
    assert parked is None
    assert svc.chat_status(principal, "async-1")["status"] == "queued"
    assert svc.work_chat() is True
    done = svc.chat_status(principal, "async-1")
    assert done["status"] == "completed"
    assert done["text"] == "later"


def test_chat_key_replays_and_rejects_a_different_body(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    svc.model.queue.append(ModelOutput(text="once", finish=True))
    first = svc.chat(principal, "cheap", None, ["hi"], "chat-1")
    second = svc.chat(principal, "cheap", None, ["hi"], "chat-1")
    assert second.text == first.text
    assert svc.model.seen.count("hi") == 1
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "cheap", None, ["other"], "chat-1")
    assert caught.value.status == 409


def test_spend_cap_stops_the_next_call(svc):
    org = svc.create_org("acme")
    created = svc.create_project(org.id, "desk", 100, cost_limit_usd=1)
    raw = svc.issue_key(created.id, "developer", ("*",))
    principal = svc.store.authenticate(raw)
    assert principal is not None
    _route(svc, principal, price=1_000_000)
    svc.model.queue.append(ModelOutput(text="ok", input_tokens=1, output_tokens=1))
    assert svc.chat(principal, "cheap", None, ["hi"]).text == "ok"
    svc.model.queue.append(ModelOutput(text="again"))
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "cheap", None, ["later"])
    assert caught.value.status == 429


def test_cancel_before_commit_drops_the_write(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
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
            allowed_tools=("note",),
            members=(),
            entrypoint="",
            steps=(),
            budget=budget(),
        ),
    )

    def cancel_first(args):
        run = next(iter(svc.store.runs.values()))
        svc.store.cancel_run(run.project_id, run.id)
        return dict(args)

    svc.tools.handlers[("*", "note")] = cancel_first
    svc.model.queue.append(tool_call("note", {"title": "ticket"}))
    run = svc.start_run(principal, agent.id, "note")
    assert run.status == "cancelled"
    assert svc.records == []
    assert svc.store.effects == {}


def test_rejected_output_fails_the_run(svc):
    class Rejecting:
        def complete(self, model, messages, tools=()):
            del model, messages, tools
            raise ProviderRejected("mdl")

    principal, _raw = project(svc)
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, workflow(principal, prompt, ""))
    svc.gateway.model = Rejecting()
    run = svc.start_run(principal, agent.id, "hello")
    assert run.status == "failed"
    assert run.failure == "provider"


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key: str, seconds: int) -> bool:
        del key, seconds
        return True

    def eval(self, script: str, numkeys: int, *args: object) -> int:
        del script, numkeys
        key = str(args[0])
        limit = int(args[1])
        count = self.values.get(key, 0)
        if count >= limit:
            return 0
        self.values[key] = count + 1
        return self.values[key]

    def ping(self) -> bool:
        return True


def test_redis_admits_and_mirrors_the_count(svc):
    principal, _raw = project(svc, limit=1)
    quota = RedisQuota("redis://unused", svc.store, client=_Redis())
    assert quota.consume(principal.project_id, 0) is True
    assert quota.consume(principal.project_id, 0) is False
    stored = svc.store.get_project(principal.project_id)
    assert stored is not None
    assert stored.request_count == 1


def test_window_mirror_does_not_move_backward(svc):
    principal, _raw = project(svc, limit=10)
    svc.store.note_window(principal.project_id, 120_000, 4)
    svc.store.note_window(principal.project_id, 60_000, 9)
    svc.store.note_window(principal.project_id, 120_000, 3)
    stored = svc.store.get_project(principal.project_id)
    assert stored is not None
    assert stored.window_start_ms == 120_000
    assert stored.request_count == 4


def test_usage_cursor_walks_the_ledger(svc):
    principal, _raw = project(svc)
    _route(svc, principal)
    for text in ("one", "two", "three"):
        svc.model.queue.append(ModelOutput(text=text, finish=True, input_tokens=0, output_tokens=0))
        svc.chat(principal, "cheap", None, [text])
    page, nxt = svc.usage_page(principal, 2, 0)
    assert len(page) == 2
    assert nxt == 2
    rest, end = svc.usage_page(principal, 2, nxt)
    assert len(rest) == 1
    assert end == 0


def test_concurrent_calls_share_one_spend_hold(svc):
    org = svc.create_org("acme")
    created = svc.create_project(org.id, "desk", 100, cost_limit_usd=1)
    raw = svc.issue_key(created.id, "developer", ("*",))
    principal = svc.store.authenticate(raw)
    assert principal is not None
    _route(svc, principal, price=1_000_000)
    gate = threading.Event()

    class Slow:
        def complete(self, model, messages, tools=()):
            del model, messages, tools
            assert gate.wait(2)
            return ModelOutput(text="ok", input_tokens=1, output_tokens=1)

    svc.gateway.model = Slow()
    errors: list[int] = []
    ok: list[str] = []

    def call() -> None:
        try:
            ok.append(svc.chat(principal, "cheap", None, ["hi"]).text)
        except PlatformError as exc:
            errors.append(exc.status)

    first = threading.Thread(target=call)
    first.start()
    denied = threading.Thread(target=call)
    denied.start()
    for _ in range(20):
        if errors:
            break
        threading.Event().wait(0.01)
    assert errors == [429]
    gate.set()
    first.join(timeout=2)
    denied.join(timeout=2)
    assert ok == ["ok"]


def test_chat_capacity_rejects_the_ninth_call(svc):
    principal, _raw = project(svc)
    stored = svc.store.get_project(principal.project_id)
    assert stored is not None
    stored.max_in_flight_chat = 8
    stored.max_provider_inflight = 8
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
    threads = [
        threading.Thread(target=lambda: svc.chat(principal, "cheap", None, ["hi"]))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for _ in range(8):
        assert entered.acquire(timeout=2)
    with pytest.raises(PlatformError) as caught:
        svc.chat(principal, "cheap", None, ["hi"])
    assert caught.value.status == 429
    assert caught.value.retry_after == 1
    gate.set()
    for thread in threads:
        thread.join(timeout=2)


def test_delete_cancels_a_queued_job(svc):
    principal, _raw = project(svc)
    kb = svc.create_kb(principal, "policies")
    svc.submit_document(principal, kb, "doc", 1, "one", ("*",), "job")
    svc.delete_document(principal, kb, "doc")
    assert svc.work() is None
    assert svc.knowledge.chunks == []


def test_success_settles_the_spend_hold(svc):
    org = svc.create_org("acme")
    created = svc.create_project(org.id, "desk", 100, cost_limit_usd=1)
    raw = svc.issue_key(created.id, "developer", ("*",))
    principal = svc.store.authenticate(raw)
    assert principal is not None
    _route(svc, principal, price=1_000_000)
    svc.model.queue.append(ModelOutput(text="ok", input_tokens=1, output_tokens=0))
    svc.chat(principal, "cheap", None, ["hi"])
    stored = svc.store.get_project(created.id)
    assert stored is not None
    assert stored.reserved_micros == 0
    assert stored.spend_micros > 0


def test_pepper_changes_the_stored_hash():
    from control.store import Store, hash_secret

    assert hash_secret("sk_a") != hash_secret("sk_a", "pepper")
    store = Store(pepper="pepper")
    org = store.create_org("acme")
    project = store.create_project(org.id, "desk")
    _record, raw = store.issue_key(project.id, "developer")
    assert store.authenticate(raw) is not None
    assert Store().authenticate(raw) is None


def test_spend_settle_never_crosses_the_usd_cap(svc):
    from control.records import Usage

    org = svc.create_org("acme")
    created = svc.create_project(org.id, "desk", 100, cost_limit_usd=0.5)
    held = svc.store.reserve_spend(created.id, 100_000, now_ms=1_000)
    assert held == 100_000
    svc.store.record_usage(
        Usage(
            project_id=created.id,
            model_id="mdl",
            model_name="big",
            pricing_version="2026-11",
            input_tokens=1,
            output_tokens=1,
            cost=1.0,
            status="success",
            org_id=org.id,
        ),
        release_held=held,
    )
    stored = svc.store.get_project(created.id)
    assert stored is not None
    assert stored.spend_micros == 500_000
    assert stored.reserved_micros == 0


def test_database_url_requires_a_pepper():
    from core.services import build_services

    with pytest.raises(PlatformError) as caught:
        build_services(
            {
                "database_url": "postgresql://genai:genai@localhost:5432/genai",
                "redis_url": "redis://localhost:6379/0",
                "key_pepper": "",
            }
        )
    assert caught.value.status == 500
    assert "PEPPER" in caught.value.detail


def test_auth_limiter_blocks_after_too_many_failures():
    from core.authlimit import AuthLimiter

    limiter = AuthLimiter(limit=2, window_s=60)
    assert limiter.allow("client:bad")
    limiter.fail("client:bad")
    limiter.fail("client:bad")
    assert not limiter.allow("client:bad")
