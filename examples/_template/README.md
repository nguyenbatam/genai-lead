# Template — add an agent without rebuilding the platform

Copy this folder:

```bash
cp -R examples/_template examples/my-agent
# edit examples/my-agent/agent.json
```

Then against a running stack (`make up`):

```bash
export PLATFORM_BASE_URL=http://127.0.0.1:8000
export PLATFORM_ADMIN_TOKEN=local-admin
# or reuse an existing project key:
# export PLATFORM_API_KEY=...

.venv/bin/python -m examples.my-agent.register   # after renaming package imports
```

Simpler path while iterating: keep using this module name by editing files in place, or call the HTTP API directly (see `examples/README.md`).

## Rules

1. HTTP client only — never import `control` / `core`.
2. Prefer tools already in the registry. New tools = platform change (rebuild).
3. Ship eval cases under `datasets/<name>/`.
4. Choose mode: `workflow` (fixed steps), `agent` (tool loop), `supervisor` (registered members).
