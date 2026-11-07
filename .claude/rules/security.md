---
paths:
  - "control/**"
  - "core/**"
  - "apps/**"
  - "evals/**"
  - "fixtures/**"
---

# Security

- Secrets only from the environment. Never logged, never written into an answer, never committed.
- Questions, documents, chats, and emails are untrusted. Mask PII before the planner and before any log. Prompt injection is `.claude/rules/injection.md`: withhold instruction-shaped lines, spotlight what remains, and keep authority in the allow-list. The vendored OWASP note in `backend-standards` is the checklist. The injection rule is the control.
- The model does not choose identity, project, or quota. The API key resolves to a project. A body field that names another project is ignored.
- Every tool the run may call is the allow-list stored on that agent version. Any other name is `denied` and the run stops. A side-effect tool waits for approval and carries an idempotency key.
- ACL is enforced in retrieval and again when the parent text is loaded. A cache key includes the project. The model never sees a chunk the project cannot read, and never receives a connection string or a secret.
- Model output is data: citation labels are checked against ids retrieved in this turn for this principal. Never `eval` or `exec`. Never use model text as a path or a command.
- Inputs are bounded. Subprocesses are out of scope for this prototype.
- Error text shown to a person is the outcome and the answer. Logs record workflow, outcome, tool names, token counts, and latency. They do not record the question body, the document body, or the answer body.
