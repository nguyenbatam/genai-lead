---
paths:
  - "control/**"
  - "core/**"
  - "apps/**"
  - "evals/**"
  - "fixtures/**"
  - "tests/**"
---

# Prompt injection

Adapted from `ai_deployment_platform` decision D19 (OWASP LLM01 Secure Implementation Pipeline, Microsoft Spotlighting). The pattern filter is a heuristic. It is not the authority boundary.

| Control | In this repo |
|---|---|
| Untrusted channels | Employee questions, PDF and Word bodies, chat transcripts, and email bodies. Indirect injection lives in the retrieved text, not only in the question |
| Pattern filter | `core/guard.py`. Instruction and exfil shapes, including "ignore all your previous instructions" and "bỏ qua mọi chỉ dẫn". A matching line is replaced with `[withheld: instruction-like text]` before the planner sees it. A matching passage never takes a retrieval slot |
| Second-pass model | Only text the regex allowed, and only when it is at least 40 characters. `INJECTION_SCREEN` points at `POST /screen`. The agent model is not this screener. `INJECTION_SCREEN_FAIL_CLOSED=true` withholds the text when the screener is down. CI leaves the variable unset, so the gate stays on the regex |
| Keep data separate from instructions | Model-facing text is wrapped in `<<data:…>>` / `<<end:…>>`. Those markers are stripped from the payload first, so a document cannot close the block early |
| Permissions outside the model | Tools are the allow-list on the agent version. The planner may call several or none of those. Any other name is `denied` and the run stops. The model never decides ACL, quota, or approval |
| No consequential action | A side-effect tool is off unless that agent version lists it, and then it waits for approval. Retry uses an idempotency key. Sending mail or calling the network from model text is refused |
| Model output is data | Citation labels must be ids retrieved this turn for this principal. Invented labels are stripped. Output is never a path, a command, or a recipient |
| What the model screen does not decide | A paraphrase the regex misses is why the second pass exists. The screener still does not grant a tool, invent a citation, or waive ACL. Those stay in the allow-list, the citation check, and the data-layer filter |

Do not weaken `guard.allows` to make a fixture pass. Add a case instead.
