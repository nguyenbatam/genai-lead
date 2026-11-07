from __future__ import annotations

import pytest

from control.errors import PlatformError
from control.records import AgentDef
from core.model import ModelOutput
from core.services import budget
from tests.conftest import card, project, publish, published_prompt


def test_raw_api_key_is_not_stored(svc):
    principal, raw = project(svc)
    blob = " ".join(svc.store.plaintext_secrets())
    assert raw not in blob
    assert svc.store.authenticate(raw) == principal


def test_revoked_key_cannot_authenticate(svc):
    principal, raw = project(svc)
    key_id = principal.key_id
    owner_raw = svc.issue_key(principal.project_id, "owner")
    owner = svc.store.authenticate(owner_raw)
    assert owner is not None
    svc.revoke_key(owner, key_id)
    assert svc.store.authenticate(raw) is None


def test_other_project_cannot_read_agent(svc):
    left, _raw = project(svc, "left")
    right, _raw = project(svc, "right")
    prompt = published_prompt(svc, left)
    agent = publish(
        svc,
        left,
        AgentDef(
            id="agt_left",
            project_id=left.project_id,
            name="left",
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
    with pytest.raises(PlatformError) as caught:
        svc.validate_agent(right, agent.id)
    assert caught.value.status == 404


def test_prompt_v2_publish_requires_eval_and_stays_immutable(svc):
    principal, _raw = project(svc)
    first = published_prompt(svc, principal, "version one")
    try:
        svc.publish_prompt(principal, first.id)
    except Exception as exc:
        assert getattr(exc, "detail", "") == "immutable"
    second = svc.revise_prompt(principal, first.id, "version two")
    try:
        svc.publish_prompt(principal, second.id)
        raise AssertionError("publish should fail")
    except Exception as exc:
        assert getattr(exc, "detail", "") == "eval"
    svc.mark_eval(principal, second.id, True)
    assert svc.publish_prompt(principal, second.id).status == "published"


def test_quota_stops_the_next_request(svc):
    principal, _raw = project(svc, limit=1)
    created = card(svc, principal, "small", "cheap")
    svc.set_route(principal, "cheap", created.id, None)
    svc.model.queue.append(ModelOutput(text="ok"))
    svc.chat(principal, "cheap", None, ["hi"])
    try:
        svc.chat(principal, "cheap", None, ["again"])
        raise AssertionError("quota should fail")
    except Exception as exc:
        assert getattr(exc, "status", None) == 429
        assert getattr(exc, "retry_after", 0) >= 1
