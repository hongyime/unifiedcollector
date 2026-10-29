# Task 39 — Repair-only credential policy

- Result: complete static policy; no credential action was executed.
- `src/core/credential_repair_policy.py` classifies only supplied state. Valid,
  owned, non-exposed sessions return `ok` with `preserve`; it has no rotate or
  revoke action.
- Missing, expired, misowned, exposed, revoked, and deployment-required state
  returns an explicit `repair` reason. Exposed or revoked credentials are not
  rollback-eligible.
- QR, 2FA, OAuth, PAT, Beeper-token, and GHunt-login cases return `blocked`
  with `human_action`, never `ok`.
- Validation: `uv run --no-sync pytest -q
  tests/core/test_credential_repair_policy.py
  tests/tools/test_credential_consumer_map.py` passed 17 tests.
- No value was read, no provider call occurred, and no valid credential was
  rotated, revoked, or replaced. Task 49 remains the only activation phase.
