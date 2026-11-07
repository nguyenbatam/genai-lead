"""JSON documents for Postgres. Tuples come back as lists."""

from __future__ import annotations

from dataclasses import asdict

from control.records import (
    AgentDef,
    ApiKey,
    Budget,
    Member,
    ModelCard,
    Prompt,
    Route,
    RunRecord,
    Usage,
)


def dump(value: object) -> dict:
    raw = asdict(value)  # type: ignore[arg-type]
    return raw


def load_prompt(raw: dict) -> Prompt:
    return Prompt(**raw)


def load_model(raw: dict) -> ModelCard:
    body = dict(raw)
    body["capabilities"] = tuple(body["capabilities"])
    body.setdefault("provider", "")
    body.setdefault("tier_affinity", "any")
    body.setdefault("tier_order", "free_paid_local")
    return ModelCard(**body)


def load_route(raw: dict) -> Route:
    body = dict(raw)
    ids = body.pop("fallback_model_ids", None)
    single = body.pop("fallback_model_id", None)
    if ids:
        fallbacks = tuple(str(item) for item in ids if item)
    elif single:
        fallbacks = (str(single),)
    else:
        fallbacks = ()
    body.setdefault("tier_order", "")
    return Route(
        project_id=str(body["project_id"]),
        task=str(body["task"]),
        model_id=str(body["model_id"]),
        fallback_model_ids=fallbacks,
        tier_order=str(body.get("tier_order") or ""),
    )


def load_agent(raw: dict) -> AgentDef:
    body = dict(raw)
    body["allowed_tools"] = tuple(body["allowed_tools"])
    body["steps"] = tuple(body["steps"])
    body["budget"] = Budget(**body["budget"])
    body["members"] = tuple(
        Member(
            id=item["id"],
            model_profile=item["model_profile"],
            allowed_tools=tuple(item["allowed_tools"]),
            max_turns=item["max_turns"],
        )
        for item in body["members"]
    )
    body["allowed_models"] = tuple(body.get("allowed_models") or ())
    body.setdefault("prefer_cheap", True)
    body.setdefault("max_hint_calls", 1)
    body.setdefault("max_hint_cost_usd", 0.0)
    body.setdefault("hint_canary_percent", 100)
    return AgentDef(**body)


def load_key(raw: dict) -> ApiKey:
    body = dict(raw)
    body["groups"] = tuple(body.get("groups") or ())
    return ApiKey(**body)


def load_usage(raw: dict) -> Usage:
    body = dict(raw)
    body.setdefault("credential_id", "")
    body.setdefault("tier", "")
    return Usage(**body)


def load_run(raw: dict) -> RunRecord:
    body = dict(raw)
    body["citations"] = list(body.get("citations") or [])
    body["tool_calls"] = list(body.get("tool_calls") or [])
    body["events"] = list(body.get("events") or [])
    body["passages"] = dict(body.get("passages") or {})
    body["pending_args"] = dict(body.get("pending_args") or {})
    body["approved"] = list(body.get("approved") or [])
    body["groups"] = tuple(body.get("groups") or ())
    body["applied_keys"] = list(body.get("applied_keys") or [])
    body["messages"] = list(body.get("messages") or [])
    body["member_turns"] = {
        str(key): int(value) for key, value in (body.get("member_turns") or {}).items()
    }
    body.setdefault("failure", "")
    body.setdefault("lease_ms", 0)
    body.setdefault("idempotency_key", "")
    body.setdefault("deadline_ms", 0)
    body.setdefault("finished_ms", 0)
    body.setdefault("started_ms", body.get("started_ms") or 0)
    body.setdefault("staged", None)
    body["pending_calls"] = list(body.get("pending_calls") or [])
    body.setdefault("calls_queued", False)
    body["fan_in"] = dict(body.get("fan_in") or {})
    body.setdefault("hint_calls", 0)
    body.setdefault("hint_cost", 0.0)
    body.setdefault("next_model_hint", "")
    return RunRecord(**body)
