from __future__ import annotations

import pytest

from control.records import AgentDef, ModelCard, Principal, Prompt
from control.store import new_id
from core.model import Claim, ModelOutput, ScriptedModel, ToolCall
from core.services import Services, budget, member
from core.vectors import configure_embedding


@pytest.fixture(autouse=True)
def _hash_embeddings() -> None:
    """Unit tests stay on hash embed; live tests opt into fastembed explicitly."""
    configure_embedding("hash")


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def svc(model: ScriptedModel) -> Services:
    return Services(model)


def project(
    svc: Services,
    name: str = "desk",
    limit: int = 100,
    groups: tuple[str, ...] = ("*",),
) -> tuple[Principal, str]:
    org = svc.create_org("acme")
    created = svc.create_project(org.id, name, limit)
    raw = svc.issue_key(created.id, "developer", groups)
    principal = svc.store.authenticate(raw)
    assert principal is not None
    return principal, raw


def card(svc: Services, principal: Principal, name: str, profile: str) -> ModelCard:
    created = svc.register_model(
        principal,
        ModelCard(
            id=new_id("mdl"),
            project_id=principal.project_id,
            name=name,
            profile=profile,
            capabilities=(profile,),
            input_price=0.0,
            output_price=0.0,
            pricing_version="2026-11",
        ),
    )
    return created


def published_prompt(
    svc: Services, principal: Principal, template: str = "Answer from data."
) -> Prompt:
    prompt = svc.create_prompt(principal, "answer", template)
    return svc.publish_prompt(principal, prompt.id)


def publish(svc: Services, principal: Principal, agent: AgentDef) -> AgentDef:
    svc.create_agent(principal, agent)
    return svc.publish_agent(principal, agent.id)


def workflow(principal: Principal, prompt: Prompt, kb_id: str) -> AgentDef:
    return AgentDef(
        id=new_id("agt"),
        project_id=principal.project_id,
        name="knowledge",
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


def tool_call(name: str, args: dict | None = None) -> ModelOutput:
    return ModelOutput(tool_calls=(ToolCall(name, args or {}),), input_tokens=0, output_tokens=1)


def claims_of(*pairs: tuple[str, str]) -> ModelOutput:
    return ModelOutput(
        text="ok",
        finish=True,
        claims=tuple(Claim(text, cite) for text, cite in pairs),
        input_tokens=0,
        output_tokens=1,
    )


__all__ = [
    "card",
    "claims_of",
    "member",
    "project",
    "publish",
    "published_prompt",
    "tool_call",
    "workflow",
]
