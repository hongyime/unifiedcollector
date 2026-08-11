# UnifiedCollector Agent Journal

- 2026-08-11 14:47 SGT: Kept the collector-derived recon seed direction because it moves SpiderFoot from manual-only smoke testing toward bounded operational use; manual targets remain allowlist-gated, while collector-derived targets are limited by source/type/source-table/domain-suffix policy.
- 2026-08-11 15:24 SGT: Kept SpiderFoot on `src.recon_spiderfoot_service` instead of `src.main` because the sidecar image intentionally has a slim dependency set; CLI commands belong in the full collector image.
- 2026-08-11 15:31 SGT: Allowed `.agents/` through the blanket dotfile ignore because AGENTS.md now requires committed cross-agent handoff state.
- 2026-08-11 15:41 SGT: Kept recon seed dry-run output redacted by default so operator logs can show counts, source types, target hosts, and stable hashes without leaking full raw URLs or usernames.
- 2026-08-11 15:45 SGT: Suppressed optional browser diagnostic timeouts only when useful browser content is already active; source failures and stale content still remain visible.
- 2026-08-11 16:02 SGT: Treated WhatsApp bridge partial pairing as live when at least one bridge slot is ready; empty optional slots remain visible as operator notes rather than source failures.
- 2026-08-11 16:11 SGT: Kept SpiderFoot observation dedupe on a SHA-256 `value_hash` unique index because raw observation values can be long or volatile; restored the applied migration body to match the live ledger instead of editing an already-applied migration.
- 2026-08-11 16:19 SGT: Browser media revisit queues now expire stale exhausted claimed rows to failed audit state instead of leaving them permanently claimed; this preserves evidence while keeping media recovery queues drainable.
