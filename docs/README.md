# Docs

Operational documentation for the unified collector, consolidated here so root
stays tidy. Root retains `README.md`, `AGENTS.md`, `LICENSE`, `NOTICE`,
`CONTRIBUTING.md`, `SECURITY.md`, `REPO_MAP.md`, and `AUDIT.md`.

## Layout

| Path | Purpose |
|---|---|
| `docs/audits/` | Historical + current audit artifacts, drift snapshots, recovery TODOs, cross-service sync progress |
| `docs/contracts/` | Interface contracts. Modifications are breaking — coordinate with the analyzer before changing shape. |
| `docs/KNOWN_ISSUES.md` | Living tracker of unresolved architectural concerns (Open + Resolved sections) |
| `docs/enrichment.md` | Deep reference for the OSS enrichment pipeline (maigret, phone-OSINT, GHunt, SpiderFoot, analyzer bridge) |

## Cross-repo sync note

`AGENTS.md:149` documents that the sourcerepo-driven sync intentionally removes
`docs/`, `skills/`, and `skills-lock.json` from target repositories. This
repository now holds tracked docs under `docs/` — the sourcerepo sync policy
must exempt project docs from the removal step, or a scheduled sync will delete
them. Coordinate before running the sync workflow again.
