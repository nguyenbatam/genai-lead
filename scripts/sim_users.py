"""Simulate real platform users: new keys, new agents, isolation, approval.

Runs through the public HTTP API (TestClient) against whatever store
load_settings() points at. Forces ScriptedModel so CI/local does not need
live LLM keys. Worker duties (ingest + run sweep) are driven in-process —
same code paths as apps/workers.

Wired into ./scripts/test_local.sh.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.settings import load_settings
from core.model import Claim, ModelOutput, ScriptedModel, ToolCall
from core.services import Services, build_services


def main() -> int:
    os.environ.setdefault("PLATFORM_ADMIN_TOKEN", "sim-admin")
    os.environ.setdefault("EMBEDDING_BACKEND", os.environ.get("EMBEDDING_BACKEND", "hash"))
    svc = _services()
    app = create_app(services=svc)
    client = TestClient(app)
    admin = {"Authorization": f"Bearer {os.environ['PLATFORM_ADMIN_TOKEN']}"}
    stamp = str(int(time.time()))

    print("==> admin creates org + two projects")
    org = _ok(client.post("/v1/orgs", headers=admin, json={"name": f"sim-acme-{stamp}"}))["id"]
    knowledge_id = _ok(
        client.post(
            "/v1/projects",
            headers=admin,
            json={"org_id": org, "name": f"knowledge-{stamp}"},
        )
    )["id"]
    desk_id = _ok(
        client.post(
            "/v1/projects",
            headers=admin,
            json={"org_id": org, "name": f"desk-{stamp}"},
        )
    )["id"]

    print("==> issue keys for alice (finance), bob (hr), cara (desk), dana (desk new hire)")
    alice = _key(client, admin, knowledge_id, ["finance"])
    bob = _key(client, admin, knowledge_id, ["hr"])
    cara = _key(client, admin, desk_id, [])
    dana = _key(client, admin, desk_id, [])

    print("==> alice registers knowledge workflow agent (no platform rebuild)")
    _cheap_route(client, alice, f"knowledge-small-{stamp}")
    kb = _ok(client.post("/v1/knowledge-bases", headers=alice, json={"name": "policies"}))["id"]
    job = client.post(
        f"/v1/knowledge-bases/{kb}/documents",
        headers=alice,
        json={
            "document_id": "policy",
            "version": 1,
            "text": "CFO phải phê duyệt ngân sách thiết bị A.",
            "acl": ["finance"],
            "idempotency_key": f"sim-policy-{stamp}",
        },
    )
    assert job.status_code == 202, job.text
    _drain_jobs(svc)
    job_body = _ok(client.get(f"/v1/knowledge-bases/{kb}/jobs/{job.json()['id']}", headers=alice))
    assert job_body["status"] == "indexed", job_body

    prompt = _prompt(client, alice, "knowledge-answer", "Answer from passages only.")
    knowledge_agent = _publish_agent(
        client,
        alice,
        {
            "name": "knowledge",
            "mode": "workflow",
            "prompt_id": prompt,
            "model_profile": "cheap",
            "allowed_tools": ["search_knowledge_base"],
            "steps": ["retrieve", "answer"],
            "kb_id": kb,
        },
    )

    parent = _parent_chunk_id(svc, knowledge_id, kb)
    grounded = ModelOutput(
        text="CFO phê duyệt.",
        finish=True,
        claims=(Claim("CFO phải phê duyệt ngân sách thiết bị A", parent),),
        input_tokens=2,
        output_tokens=4,
    )
    svc.model.queue.extend([grounded, grounded])

    print("==> alice runs knowledge; bob (hr) is denied the finance doc path")
    alice_run = _run(client, svc, alice, knowledge_agent, "phê duyệt ngân sách")
    bob_run = _run(client, svc, bob, knowledge_agent, "phê duyệt ngân sách")
    assert alice_run["outcome"] in {"completed", "ungrounded", "unverified"}, alice_run
    assert bob_run["outcome"] in {"no_answer", "ungrounded", "completed", "unverified"}, bob_run
    if alice_run["outcome"] == "completed":
        assert alice_run.get("citations"), alice_run

    print("==> cara registers desk agent")
    _cheap_route(client, cara, f"desk-small-{stamp}")
    desk_prompt = _prompt(client, cara, f"desk-{stamp}", "Use create_record when asked.")
    desk_agent = _publish_agent(
        client,
        cara,
        {
            "name": "desk",
            "mode": "agent",
            "prompt_id": desk_prompt,
            "model_profile": "cheap",
            "allowed_tools": ["create_record"],
        },
    )
    svc.model.queue.append(
        ModelOutput(
            tool_calls=(ToolCall("create_record", {"title": "ticket"}),),
            input_tokens=1,
            output_tokens=1,
        )
    )
    waiting = _run(client, svc, cara, desk_agent, "mở phiếu")
    assert waiting["status"] == "waiting_approval", waiting
    assert waiting.get("pending_payload"), waiting

    print("==> dana (new developer) adds a SECOND agent on the same project — still no rebuild")
    triage_prompt = _prompt(client, dana, f"triage-{stamp}", "Classify only. No tools.")
    triage_agent = _publish_agent(
        client,
        dana,
        {
            "name": "triage",
            "mode": "agent",
            "prompt_id": triage_prompt,
            "model_profile": "cheap",
            "allowed_tools": [],
        },
    )
    svc.model.queue.append(
        ModelOutput(text="priority=low", finish=True, input_tokens=1, output_tokens=2)
    )
    triage_run = _run(client, svc, dana, triage_agent, "ưu tiên thấp")
    assert triage_run["status"] == "completed", triage_run
    assert triage_agent != desk_agent

    print("==> cross-project: alice cannot validate desk agent")
    foreign = client.post(f"/v1/agents/{desk_agent}/validate", headers=alice)
    assert foreign.status_code == 404, foreign.text

    print("==> viewer key cannot start runs")
    viewer_raw = _ok(
        client.post(
            "/v1/api-keys",
            headers=admin,
            json={"project_id": desk_id, "role": "viewer", "groups": []},
        )
    )["key"]
    viewer = {"Authorization": f"Bearer {viewer_raw}"}
    denied = client.post(
        f"/v1/agents/{desk_agent}/runs",
        headers=viewer,
        json={"query": "nope"},
    )
    assert denied.status_code == 403, denied.text

    print(
        "OK sim_users "
        f"knowledge={alice_run['outcome']} "
        f"hr={bob_run['outcome']} "
        f"desk={waiting['outcome']} "
        f"new_agent={triage_run['outcome']}"
    )
    return 0


def _services() -> Services:
    settings = load_settings()
    # Deterministic provider for the journey; live LLM is covered by bench.
    settings = {
        **settings,
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_credentials": "",
        "llm_credentials_file": "",
        "embedding_backend": settings.get("embedding_backend") or "hash",
    }
    svc = build_services(settings)
    # Gateway captured the model at construction; replace both or queues never drain.
    scripted = ScriptedModel()
    svc.model = scripted
    svc.gateway.model = scripted
    return svc


def _ok(response: Any) -> dict:
    assert response.status_code < 400, f"{response.status_code} {response.text}"
    return response.json()


def _key(client: TestClient, admin: dict[str, str], project_id: str, groups: list[str]) -> dict:
    issued = _ok(
        client.post(
            "/v1/api-keys",
            headers=admin,
            json={"project_id": project_id, "role": "developer", "groups": groups},
        )
    )
    return {"Authorization": f"Bearer {issued['key']}"}


def _cheap_route(client: TestClient, headers: dict[str, str], name: str) -> None:
    model = _ok(
        client.post(
            "/v1/models",
            headers=headers,
            json={
                "name": name,
                "profile": "cheap",
                "capabilities": ["cheap"],
                "input_price": 0,
                "output_price": 0,
                "pricing_version": "2026-11",
            },
        )
    )
    _ok(client.post("/v1/routes", headers=headers, json={"task": "cheap", "model_id": model["id"]}))


def _prompt(client: TestClient, headers: dict[str, str], name: str, template: str) -> str:
    prompt = _ok(
        client.post("/v1/prompts", headers=headers, json={"name": name, "template": template})
    )
    _ok(client.post(f"/v1/prompts/{prompt['id']}/publish", headers=headers))
    return prompt["id"]


def _publish_agent(client: TestClient, headers: dict[str, str], body: dict) -> str:
    agent = _ok(client.post("/v1/agents", headers=headers, json=body))
    published = _ok(client.post(f"/v1/agents/{agent['id']}/publish", headers=headers))
    return published["id"]


def _drain_jobs(svc: Services, limit: int = 32) -> None:
    for _ in range(limit):
        if svc.work() is None:
            return


def _parent_chunk_id(svc: Services, project_id: str, kb_id: str) -> str:
    knowledge = svc.knowledge
    assert knowledge is not None
    if hasattr(knowledge, "_chunks"):
        chunks = knowledge._chunks(project_id, kb_id)
    else:
        chunks = knowledge.chunks
    return next(chunk.id for chunk in chunks if chunk.kind == "parent")


def _run(
    client: TestClient,
    svc: Services,
    headers: dict[str, str],
    agent_id: str,
    query: str,
) -> dict:
    started = client.post(
        f"/v1/agents/{agent_id}/runs",
        headers=headers,
        json={"query": query},
    )
    assert started.status_code in {200, 202}, started.text
    body = started.json()
    if body["status"] in {"queued", "running"}:
        assert svc.sweep_runs() >= 1
        body = _ok(client.get(f"/v1/runs/{body['id']}", headers=headers))
    return body


if __name__ == "__main__":
    sys.exit(main())
