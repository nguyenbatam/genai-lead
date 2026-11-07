---
paths:
  - "tests/**"
  - "evals/**"
---

# Testing

- Name tests by behaviour: `test_<condition>_<expected>`.
- Unit tests use in-memory adapters behind the same ports as Qdrant, Mongo, Redis, Postgres, and Kafka. No Docker, no model API. Each test < 1s. The Stop hook runs `pytest -q -m 'not integration'`.
- Integration tests are marked `integration`, require Compose, and are not part of the Stop hook.
- A scripted planner proves the gate. Do not describe its pass rate as model quality.
- Every bug fix starts with a failing test.
- Must-have cases: a key for project A cannot read project B; a raw API key is not stored; a completion records usage against the project that owns the key; fallback runs only when the task policy allows it; an ungranted tool is `denied`; a side-effect retry does not apply twice; a supervisor cannot loop past the step cap; a second project cannot retrieve the first project's parent; an updated document does not leave the old chunks searchable; an ungrounded claim is not `completed`; an injection line inside a document is withheld; the eval gate fails on a leak or an unauthorized tool.
