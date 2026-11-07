# Knowledge (sample app)

Mode `workflow`: retrieve → answer. Shared RAG service, citations, no write tools.

Eval cases: `datasets/knowledge/`.

## Contract

- HTTP only. Do not import `control` or `core`.
- Adding this agent does **not** rebuild the platform image.
- Platform must already be up (`make up`).

## Run

```bash
# terminal 1
make up

# terminal 2 — from repo root, with .env loaded
export PLATFORM_BASE_URL=http://127.0.0.1:8000
export PLATFORM_ADMIN_TOKEN=local-admin   # from .env

.venv/bin/python -m examples.knowledge.register
# export PLATFORM_API_KEY=...  # printed once on bootstrap

.venv/bin/python -m examples.knowledge.run
.venv/bin/python -m examples.knowledge.run "câu hỏi khác"
```

With a live LLM (`LLM_CREDENTIALS_FILE` / `make up-local-llm`) answers are real. Without credentials the API still accepts the agent; completion quality depends on the scripted/empty provider.

## Files

| File | Role |
|---|---|
| `agent.json` | Manifest (mode, tools, steps, sample doc) |
| `register.py` | Create KB, ingest, prompt, agent, publish |
| `run.py` | `POST .../runs` and print outcome |
| `.local_state.json` | Local ids + key (gitignored) |
