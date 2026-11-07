"""Draft, validate, publish. Published prompt and agent versions are not edited in place."""

from __future__ import annotations

from control.records import AgentDef, Prompt
from control.vocab import (
    MODE_SUPERVISOR,
    MODE_WORKFLOW,
    MODES,
    PATTERNS,
    PUBLISH_PUBLISHED,
    PUBLISH_STATUSES,
)


def prompt_block(prompt: Prompt) -> str | None:
    if prompt.status == PUBLISH_PUBLISHED:
        return "immutable"
    if prompt.version > 1 and not prompt.eval_passed:
        return "eval"
    if "sk_" in prompt.template:
        return "secret"
    return None


def validate(agent: AgentDef, prompt: Prompt | None, tool_names: set[str]) -> list[str]:
    errors: list[str] = []
    if agent.mode not in MODES:
        errors.append("mode")
    if agent.pattern == "hierarchical":
        errors.append("hierarchical")
    elif agent.pattern not in PATTERNS:
        errors.append("pattern")
    if agent.status not in PUBLISH_STATUSES:
        errors.append("status")
    if (
        prompt is None
        or prompt.status != PUBLISH_PUBLISHED
        or prompt.project_id != agent.project_id
    ):
        errors.append("prompt")
    elif "sk_" in prompt.template:
        errors.append("secret")
    for name in agent.allowed_tools:
        if name not in tool_names:
            errors.append(f"tool:{name}")
    budget = agent.budget
    if (
        budget.max_steps < 1
        or budget.max_tokens < 1
        or budget.max_wall_ms < 1
        or budget.max_handoffs < 1
        or budget.max_parallel < 1
        or budget.max_cost_usd <= 0
    ):
        errors.append("budget")
    if agent.mode == MODE_WORKFLOW and not agent.steps:
        errors.append("steps")
    if agent.mode == MODE_SUPERVISOR:
        ids = {member.id for member in agent.members}
        if not ids or agent.entrypoint not in ids:
            errors.append("entrypoint")
        for member in agent.members:
            for name in member.allowed_tools:
                if name not in tool_names:
                    errors.append(f"tool:{name}")
    return errors
