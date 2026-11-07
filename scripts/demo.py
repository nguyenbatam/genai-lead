"""Two projects, one platform process: knowledge workflow and desk approval."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.model import Claim, ModelOutput, ToolCall


def main() -> None:
    os.environ.setdefault("PLATFORM_ADMIN_TOKEN", "demo-admin")
    app = create_app()
    client = TestClient(app)
    admin = {"Authorization": f"Bearer {os.environ['PLATFORM_ADMIN_TOKEN']}"}
    org = client.post("/v1/orgs", headers=admin, json={"name": "acme"}).json()["id"]
    knowledge_id = client.post(
        "/v1/projects", headers=admin, json={"org_id": org, "name": "knowledge"}
    ).json()["id"]
    knowledge = _key(client, admin, knowledge_id, ["finance"])
    hidden_key = _key(client, admin, knowledge_id, ["hr"])
    desk_id = client.post(
        "/v1/projects", headers=admin, json={"org_id": org, "name": "desk"}
    ).json()["id"]
    desk = _key(client, admin, desk_id, [])
    _model(client, knowledge)
    _model(client, desk)

    created = client.post("/v1/knowledge-bases", headers=knowledge, json={"name": "policies"})
    kb = created.json()["id"]
    client.post(
        f"/v1/knowledge-bases/{kb}/documents",
        headers=knowledge,
        json={
            "document_id": "policy",
            "version": 1,
            "text": "CFO phải phê duyệt ngân sách thiết bị A.",
            "acl": ["finance"],
            "idempotency_key": "policy-1",
        },
    )
    app.state.svc.work()
    parent = next(chunk.id for chunk in app.state.svc.knowledge.chunks if chunk.kind == "parent")
    prompt = _prompt(client, knowledge)
    agent = client.post(
        "/v1/agents",
        headers=knowledge,
        json={
            "name": "knowledge",
            "mode": "workflow",
            "prompt_id": prompt,
            "model_profile": "cheap",
            "allowed_tools": ["search_knowledge_base"],
            "steps": ["retrieve", "answer"],
            "kb_id": kb,
        },
    ).json()["id"]
    client.post(f"/v1/agents/{agent}/publish", headers=knowledge)
    grounded = ModelOutput(
        text="CFO phê duyệt.",
        finish=True,
        claims=(Claim("CFO phải phê duyệt ngân sách thiết bị A", parent),),
        input_tokens=0,
        output_tokens=4,
    )
    app.state.svc.model.queue.extend([grounded, grounded])
    answered = client.post(
        f"/v1/agents/{agent}/runs",
        headers=knowledge,
        json={"query": "phê duyệt ngân sách"},
    ).json()
    hidden = client.post(
        f"/v1/agents/{agent}/runs",
        headers=hidden_key,
        json={"query": "phê duyệt ngân sách"},
    ).json()

    desk_prompt = _prompt(client, desk)
    desk_agent = client.post(
        "/v1/agents",
        headers=desk,
        json={
            "name": "desk",
            "mode": "agent",
            "prompt_id": desk_prompt,
            "model_profile": "cheap",
            "allowed_tools": ["create_record"],
        },
    ).json()["id"]
    client.post(f"/v1/agents/{desk_agent}/publish", headers=desk)
    app.state.svc.model.queue.append(
        ModelOutput(
            tool_calls=(ToolCall("create_record", {"title": "ticket"}),),
            input_tokens=0,
            output_tokens=1,
        )
    )
    waiting = client.post(
        f"/v1/agents/{desk_agent}/runs",
        headers=desk,
        json={"query": "mở phiếu"},
    ).json()
    print(f"knowledge outcome={answered['outcome']} citations={answered['citations']}")
    print(f"hr outcome={hidden['outcome']}")
    print(f"desk outcome={waiting['outcome']} pending={bool(waiting['pending_payload'])}")

    chain = _resilience(client, desk, app)
    summary = client.get("/v1/usage/summary", headers=desk).json()
    hints = {item["model"]: item["hint"] for item in summary.get("models", [])}
    print(
        f"resilience statuses={chain['statuses']} "
        f"fallback_from={chain['fallback_from']} "
        f"metrics_fallback={'genai_provider_fallbacks_total' in chain['metrics']}"
    )
    print(f"usage summary hints={hints}")


def _resilience(client: TestClient, headers: dict[str, str], app) -> dict:
    # Empty provider keeps the scripted demo model; pool keys are for live providers.
    primary = client.post(
        "/v1/models",
        headers=headers,
        json={
            "name": "cloud-primary",
            "profile": "reason",
            "capabilities": ["reason"],
            "input_price": 0,
            "output_price": 0,
            "pricing_version": "2026-11",
            "tier_order": "free_local_paid",
        },
    ).json()["id"]
    paid = client.post(
        "/v1/models",
        headers=headers,
        json={
            "name": "cloud-paid",
            "profile": "reason",
            "capabilities": ["reason"],
            "input_price": 0,
            "output_price": 0,
            "pricing_version": "2026-11",
        },
    ).json()["id"]
    local = client.post(
        "/v1/models",
        headers=headers,
        json={
            "name": "local-llama",
            "profile": "reason",
            "capabilities": ["reason"],
            "input_price": 0,
            "output_price": 0,
            "pricing_version": "2026-11",
            "provider": "selfhost",
        },
    ).json()["id"]
    client.post(
        "/v1/routes",
        headers=headers,
        json={
            "task": "reason",
            "model_id": primary,
            "fallback_model_ids": [paid, local],
            "tier_order": "free_local_paid",
        },
    )
    app.state.svc.model.down.update({primary, paid})
    app.state.svc.model.queue.append(
        ModelOutput(text="local saved the run", finish=True, input_tokens=1, output_tokens=1)
    )
    chat = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"task": "reason", "messages": ["provider outage"]},
    )
    assert chat.status_code == 200, chat.text
    usage = client.get("/v1/usage", headers=headers).json()["rows"]
    reason_rows = [
        row for row in usage if row["model"] in {"cloud-primary", "cloud-paid", "local-llama"}
    ]
    metrics = client.get("/metrics").text
    return {
        "statuses": [row["status"] for row in reason_rows[-3:]],
        "fallback_from": reason_rows[-1].get("fallback_from"),
        "metrics": metrics,
    }


def _key(
    client: TestClient, admin: dict[str, str], project_id: str, groups: list[str]
) -> dict[str, str]:
    issued = client.post(
        "/v1/api-keys",
        headers=admin,
        json={"project_id": project_id, "role": "developer", "groups": groups},
    )
    return {"Authorization": f"Bearer {issued.json()['key']}"}


def _model(client: TestClient, headers: dict[str, str]) -> None:
    model_id = client.post(
        "/v1/models",
        headers=headers,
        json={
            "name": "small",
            "profile": "cheap",
            "capabilities": ["cheap"],
            "input_price": 0,
            "output_price": 0,
            "pricing_version": "2026-11",
        },
    ).json()["id"]
    client.post("/v1/routes", headers=headers, json={"task": "cheap", "model_id": model_id})


def _prompt(client: TestClient, headers: dict[str, str]) -> str:
    prompt_id = client.post(
        "/v1/prompts", headers=headers, json={"name": "answer", "template": "Use the data."}
    ).json()["id"]
    client.post(f"/v1/prompts/{prompt_id}/publish", headers=headers)
    return prompt_id


if __name__ == "__main__":
    main()
