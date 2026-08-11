# UnifiedCollector Agent Journal

- 2026-08-11 14:47 SGT: Kept the collector-derived recon seed direction because it moves SpiderFoot from manual-only smoke testing toward bounded operational use; manual targets remain allowlist-gated, while collector-derived targets are limited by source/type/source-table/domain-suffix policy.
- 2026-08-11 15:24 SGT: Kept SpiderFoot on `src.recon_spiderfoot_service` instead of `src.main` because the sidecar image intentionally has a slim dependency set; CLI commands belong in the full collector image.
- 2026-08-11 15:31 SGT: Allowed `.agents/` through the blanket dotfile ignore because AGENTS.md now requires committed cross-agent handoff state.
- 2026-08-11 15:41 SGT: Kept recon seed dry-run output redacted by default so operator logs can show counts, source types, target hosts, and stable hashes without leaking full raw URLs or usernames.
