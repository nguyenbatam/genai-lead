"""Execute datasets/security/cases.jsonl with ScriptedModel — no live LLM."""

from __future__ import annotations

from control.records import AgentDef
from control.store import new_id
from core.eval import score_run
from core.model import Claim, ModelOutput
from core.services import budget
from scripts.bench_lib import DATASETS, load_jsonl
from tests.conftest import card, project, publish, published_prompt, tool_call


def _route(svc, principal):
    created = card(svc, principal, "small", "cheap")
    svc.set_route(principal, "cheap", created.id, None)
    return created


def _desk_agent(principal, prompt, tools: tuple[str, ...]) -> AgentDef:
    return AgentDef(
        id=new_id("agt"),
        project_id=principal.project_id,
        name="security-desk",
        version=1,
        status="draft",
        mode="agent",
        pattern="",
        prompt_id=prompt.id,
        model_profile="cheap",
        allowed_tools=tools,
        members=(),
        entrypoint="",
        steps=(),
        budget=budget(),
        kb_id="",
    )


def _knowledge_agent(principal, prompt, kb_id: str) -> AgentDef:
    return AgentDef(
        id=new_id("agt"),
        project_id=principal.project_id,
        name="security-knowledge",
        version=1,
        status="draft",
        mode="workflow",
        pattern="",
        prompt_id=prompt.id,
        model_profile="cheap",
        allowed_tools=("search_knowledge_base",),
        members=(),
        entrypoint="",
        steps=("retrieve", "answer"),
        budget=budget(),
        kb_id=kb_id,
    )


def _ungranted_tool_name(query: str) -> str:
    lowered = query.lower()
    for name in (
        "secret_exfil",
        "drop_database",
        "shell_exec",
        "search_other_project_kb",
        "exfiltrate_chunks",
        "shell",
    ):
        if name in lowered:
            return name
    return "secret_exfil"


def test_security_dataset_denied_cases_are_denied(svc):
    cases = [
        case
        for case in load_jsonl(DATASETS / "security" / "cases.jsonl")
        if case.get("expect_outcome") == "denied"
    ]
    assert len(cases) >= 5
    principal, _ = project(svc, "sec-denied")
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _desk_agent(principal, prompt, tools=()))
    for case in cases:
        tool = _ungranted_tool_name(str(case["query"]))
        svc.model.queue.append(tool_call(tool, {}))
        run = svc.start_run(principal, agent.id, str(case["query"]))
        assert run.outcome == "denied", case["id"]
        scored = score_run(run, expect_outcome="denied")
        assert scored["unauthorized_tool"] is True
        if case.get("expect_no_leak"):
            assert scored["leak"] is False


def test_security_dataset_waiting_approval_side_effect(svc):
    cases = [
        case
        for case in load_jsonl(DATASETS / "security" / "cases.jsonl")
        if case.get("expect_outcome") == "waiting_approval"
    ]
    assert cases
    principal, _ = project(svc, "sec-approve")
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _desk_agent(principal, prompt, tools=("create_record",)))
    for case in cases:
        svc.model.queue.append(tool_call("create_record", {"title": "sec"}))
        run = svc.start_run(principal, agent.id, str(case["query"]))
        assert run.outcome == "waiting_approval", case["id"]
        assert run.pending_payload
        assert svc.records == []


def test_security_dataset_acl_no_answer_has_empty_retrieval(svc):
    cases = [
        case
        for case in load_jsonl(DATASETS / "security" / "cases.jsonl")
        if case.get("expect_outcome") == "no_answer"
    ]
    assert cases
    principal, _ = project(svc, "sec-acl", groups=("finance",))
    kb = svc.create_kb(principal, "hr-secret")
    svc.submit_document(
        principal,
        kb,
        "payroll",
        1,
        "Bảng lương nhân viên HR là bí mật tuyệt đối.",
        ("hr",),
        "payroll-1",
    )
    assert svc.work().status == "indexed"
    for case in cases:
        groups = tuple(case.get("groups") or ("finance",))
        hits = svc.knowledge.search(principal.project_id, kb, str(case["query"]), groups)
        assert hits == [], case["id"]


def test_security_dataset_completed_no_leak_scorer(svc):
    cases = [
        case
        for case in load_jsonl(DATASETS / "security" / "cases.jsonl")
        if case.get("expect_outcome") == "completed" and case.get("expect_no_leak")
    ]
    assert len(cases) >= 3
    principal, _ = project(svc, "sec-noleak")
    _route(svc, principal)
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _desk_agent(principal, prompt, tools=()))
    for case in cases:
        svc.model.queue.append(
            ModelOutput(
                text="CFO phê duyệt theo chính sách.",
                finish=True,
                input_tokens=1,
                output_tokens=1,
            )
        )
        run = svc.start_run(principal, agent.id, str(case["query"]))
        # Desk without RAG claims may finish ungrounded; leak check is the gate.
        assert run.outcome in {"completed", "no_answer", "ungrounded"}, case["id"]
        scored = score_run(run, expect_outcome=run.outcome)
        assert scored["leak"] is False, case["id"]


def test_security_dataset_invented_cite_is_not_grounded(svc):
    cases = [
        case
        for case in load_jsonl(DATASETS / "security" / "cases.jsonl")
        if case.get("expect_outcome") in {"ungrounded", "unverified"}
    ]
    assert cases
    principal, _ = project(svc, "sec-ungrounded", groups=("finance",))
    _route(svc, principal)
    kb = svc.create_kb(principal, "policies")
    svc.submit_document(
        principal,
        kb,
        "policy",
        1,
        "CFO phải phê duyệt ngân sách thiết bị loại A.",
        ("finance",),
        "pol-1",
    )
    assert svc.work().status == "indexed"
    prompt = published_prompt(svc, principal)
    agent = publish(svc, principal, _knowledge_agent(principal, prompt, kb))
    for case in cases:
        for _ in range(2):
            svc.model.queue.append(
                ModelOutput(
                    text="made up",
                    finish=True,
                    claims=(Claim("invented", "p-fake"),),
                    input_tokens=1,
                    output_tokens=1,
                )
            )
        run = svc.start_run(principal, agent.id, str(case["query"]))
        assert run.outcome in {"ungrounded", "unverified"}, (case["id"], run.outcome)
        assert run.outcome != "completed"


def test_security_dataset_file_is_complete():
    cases = load_jsonl(DATASETS / "security" / "cases.jsonl")
    assert len(cases) >= 15
    outcomes = {str(case.get("expect_outcome")) for case in cases}
    assert {"denied", "completed", "no_answer", "waiting_approval"} <= outcomes
    assert outcomes & {"ungrounded", "unverified"}
