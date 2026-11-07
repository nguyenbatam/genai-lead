from __future__ import annotations

from control.records import AgentDef, RunRecord
from control.store import new_id
from core.eval import score_run
from core.model import ModelOutput
from core.services import budget
from tests.conftest import card, project, publish, published_prompt


def test_score_run_latency_uses_finished_not_deadline():
    run = RunRecord(
        id="run_1",
        project_id="prj_1",
        agent_id="agt_1",
        status="completed",
        outcome="no_answer",
        answer="",
        started_ms=1_000,
        finished_ms=1_250,
        deadline_ms=61_000,
    )
    scored = score_run(run, expect_outcome="no_answer")
    assert scored["latency_ms"] == 250


def test_eval_prefers_cheaper_successful_experiment(svc):
    principal, _raw = project(svc)
    prompt = published_prompt(svc, principal)
    cheap = card(svc, principal, "small", "cheap")
    svc.set_route(principal, "cheap", cheap.id, None)
    agent = publish(
        svc,
        principal,
        AgentDef(
            id=new_id("agt"),
            project_id=principal.project_id,
            name="solo",
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
    svc.model.queue.append(
        ModelOutput(text="[NO_ANSWER]", finish=True, input_tokens=1, output_tokens=1)
    )
    job = svc.submit_eval(
        principal,
        "modes",
        [{"query": "q", "expect_outcome": "no_answer"}],
        [{"name": "agent", "agent_id": agent.id}],
    )
    assert job["status"] == "queued"
    done = svc.work_eval()
    assert done is not None
    assert done["status"] == "completed"
    assert done["report"]["preferred"] == "agent"
    assert done["report"]["gate_red"] is False
    assert svc.eval_report(principal, job["id"])["status"] == "completed"
    case = done["report"]["experiments"][0]["cases"][0]
    assert case["latency_ms"] >= 0
    assert case["latency_ms"] < 60_000


def test_reserved_hold_expires_on_reconcile(svc):
    org = svc.create_org("acme")
    created = svc.create_project(org.id, "desk", 100, cost_limit_usd=1)
    held = svc.store.reserve_spend(created.id, 500_000, now_ms=1_000)
    assert held == 500_000
    project = svc.store.get_project(created.id)
    assert project is not None
    assert project.reserved_micros == 500_000
    assert svc.store.reconcile_reserved(1_000) == 0
    assert svc.store.reconcile_reserved(200_000) == 1
    cleared = svc.store.get_project(created.id)
    assert cleared is not None
    assert cleared.reserved_micros == 0
