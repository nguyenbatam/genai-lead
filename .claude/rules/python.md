---
paths:
  - "**/*.py"
  - "**/pyproject.toml"
---

# Python

- Python 3.13, venv in `.venv/`. Run tools via `.venv/bin/python -m ...`.
- Format and lint with ruff (`ruff format`, `ruff check --fix`); config in `pyproject.toml`:
  line-length 100, `select = ["E", "F", "I", "UP", "B"]`.
- Type hints on public functions; `X | None`, not `Optional[X]`.
- Fixed system vocab (run status, outcome, role, mode): one named set
  (`frozenset`, `Literal`, or `StrEnum`). Do not scatter magic strings.
  Prefer `control/vocab.py` (re-exported from `control/store.py` / `core/define.py`
  where call sites already import those). Keep Postgres status columns as `text`
  with CHECK constraints — do not map them to smallint.
- Wire and DB fields stay `str`. Parse or validate at the boundary; tolerate
  unknown values on responses when the API is open for evolution.
  Do not Enum dynamic or external values (ids, emails, free text, model ids).
- Catch specific exceptions; never `except Exception: pass`.
- Unit tests live in `tests/`, use in-memory adapters, no Docker, each test < 1s. Integration tests are marked `integration`.
- Pin exact dependency versions in `requirements.txt`.
