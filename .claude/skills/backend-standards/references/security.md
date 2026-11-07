# Security

Sources: OWASP API Security Top 10 (2023) · OWASP Top 10 for LLM Applications · 12-Factor (config) ·
principle of least privilege.

## OWASP API Top 10 (2023) — check each
1. **Broken Object Level Authorization** — every access to `/{id}` checks the caller owns/may see it.
2. **Broken Authentication** — standard mechanisms (OAuth2/OIDC, signed tokens); no custom crypto.
3. **Broken Object Property Level Authorization** — explicit response models; no mass assignment.
4. **Unrestricted Resource Consumption** — limits on body size, page size, rate, job count, runtime, cost.
5. **Broken Function Level Authorization** — admin/internal routes separated and checked.
6. **Unrestricted Access to Sensitive Business Flows** — abuse limits on expensive flows (e.g. generation).
7. **SSRF** — never fetch user-supplied URLs without an allowlist.
8. **Security Misconfiguration** — no debug in prod, strict CORS, security headers, no default creds.
9. **Improper Inventory Management** — documented, versioned endpoints; old versions retired.
10. **Unsafe Consumption of APIs** — treat third-party / LLM responses as untrusted input; validate them.

## LLM-specific (OWASP LLM Top 10)
- **Prompt injection**: user text is data, never instructions; restrict to allowlisted tasks; keep system
  prompts server-side.
- **Insecure output handling**: model output is parsed into a schema and validated; never executed,
  never used as a path, command or SQL.
- **Unbounded consumption**: cap tokens, attempts, concurrency and cost per request/user.

## Basics
- Secrets from environment/secret manager; never in code, logs, responses or the repo.
- Validate all input at the boundary (type, length, range, format); reject early.
- Subprocesses with argument lists, no shell; file paths resolved and confined to a root.
- Dependencies pinned and scanned (`pip-audit`, `govulncheck`, `cargo audit`, `npm audit`).
- Least privilege for service accounts, DB users and tokens.
