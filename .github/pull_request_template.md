## Summary

<!-- Why this change exists. 1–3 bullets. -->

## Test plan

- [ ] `pytest -q -m 'not integration and not live'` green locally
- [ ] `ruff check . && ruff format --check .` clean
- [ ] Behaviour change has a test
- [ ] If isolation touched: one cross-project denial case
- [ ] If safety touched: one refusal or leak case
