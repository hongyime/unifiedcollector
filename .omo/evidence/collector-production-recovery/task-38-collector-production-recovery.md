# Task 38 — Credential and consumer repair map

- Result: static map complete; credential activation remains gated by the later
  recovery authority and repair-only phases.
- Map: `config/credential-consumer-map.json`, schema version 1. It contains 35
  documented credential names with a required, optional, or human-controlled
  scope; their example-file source; their exact Compose consumer selector; a
  value-free validation command; and a dependency description.
- Live presence is explicitly `not_inspected`: the map reads only tracked
  `*.env.example` files and Compose wiring. It does not open real env files,
  credential stores, browser profiles, cookies, or token values. Human actions
  are marked `human-controlled`; a later repair classifier determines missing,
  expired, exposed, or misowned state without rotating valid credentials.
- Validation: `uv run --no-sync pytest -q
  tests/tools/test_credential_consumer_map.py` passed 3 tests. The verifier
  rejects a missing row and a newly introduced unmapped credential; direct
  execution reports `OK: 35 documented credentials; no values read`.
- No credential was read, changed, rotated, revoked, or tested against a live
  provider. This evidence does not authorize any service startup.
