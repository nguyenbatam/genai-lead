# Vendored skills

Brought across from `studio-kit` (originally `example_python` where the skill is generic). Project rules in `AGENTS.md` and `.claude/rules/` win on conflict.

| Skill | Source | Why it is here |
|---|---|---|
| verification-before-completion | https://github.com/obra/superpowers (MIT) | Do not claim the suite is green without running it |
| systematic-debugging | https://github.com/obra/superpowers (MIT) | Root cause before a fix |
| python-testing-patterns | https://github.com/wshobson/agents (MIT) | pytest layout and fakes |
| backend-standards | written for `example_python`, kept as a checklist | API, reliability, data, observability, security, architecture. Sources named in each reference: RFC 9110/9457, Google AIP, OWASP API and LLM Top 10, Google SRE, 12-Factor |

Not brought across, because they belong to the chemistry-video service: `fastapi` skill, `verify-e2e`, `implement-phase`, and the rules for generation, jobs, store, and that service's HTTP contract.
