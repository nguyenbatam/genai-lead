# Desk (sample app)

Mode `agent`: one write tool (`create_record`) with `requires_approval`. Retry must not double-apply.

Eval cases: `datasets/desk/`.

## Contract

- HTTP only. Do not import `control` or `core`.
- Adding this agent does **not** rebuild the platform image.
- Platform must already be up (`make up`). Worker must be running so queued runs advance.

## Run

```bash
make up
export PLATFORM_BASE_URL=http://127.0.0.1:8000
export PLATFORM_ADMIN_TOKEN=local-admin

.venv/bin/python -m examples.desk.register
.venv/bin/python -m examples.desk.run
```

Live LLM required for the model to choose `create_record`. Without credentials the run may finish as `no_answer`.

## Files

| File | Role |
|---|---|
| `agent.json` | Manifest |
| `register.py` | Prompt + agent + publish |
| `run.py` | Start run, stop at approval |
| `.local_state.json` | Local ids + key (gitignored) |
