# Bucket A refactor plans

Larger deferred refactors from AUDIT.md Section 14 "Bucket A". Each plan is
sized, sequenced, and rollback-safe. Not attempted during the fix-all sweep
because each is a multi-day dedicated sprint.

| Plan | Findings | Effort | Confidence |
|---|---|---|---|
| [perf-file-splits.md](perf-file-splits.md) | PERF-002, PERF-003, PERF-004 | 10–14 dev days | HIGH / MEDIUM / MEDIUM |
| [scheduler-refactor.md](scheduler-refactor.md) | LOGIC-005 | 6–8 dev days | HIGH |
| [dashboard-root-consolidation.md](dashboard-root-consolidation.md) | STRUCT-006 | 2–3 dev days | HIGH |
| [per-service-env-split.md](per-service-env-split.md) | SEC-003 | 4–6 dev days | MEDIUM-HIGH |
| [extension-bundler.md](extension-bundler.md) | FE-002 | 4–6 dev days | MEDIUM-HIGH |

## Recommended execution order

1. **dashboard-root-consolidation.md** — smallest, unblocks nothing but reduces friction elsewhere.
2. **per-service-env-split.md** — real security win; touches compose in a way that will conflict with any later structural change to services, so do it before the file splits.
3. **perf-file-splits.md** — 4A/4B/4C in that order (dashboard first, then ig_ingest, then telegram). Each sub-plan is independent so they can be sequenced separately.
4. **scheduler-refactor.md** — clean win; independent of the others.
5. **extension-bundler.md** — do last; frontend concern that doesn't block backend work.
