# UNIFIEDCOLLECTOR — Full Per-Package Collector Refactor (EXECUTING)

Decision (user): split ALL 11 collectors into per-package structure for uniform
layout + easier changes. Not just the two giants.

## Target structure
  src/collectors/<name>.py  ->  src/collectors/<name>/
                                    __init__.py     (re-exports <Name>Collector)
                                    collector.py    (the orchestrator class)
                                    parse.py        (pure extract/transform)
                                    persist.py      (DB upserts)
                                    media.py        (downloads)         [if present]
                                    transport.py    (http/client/proxy) [if present]
                                    auth.py         (login/session)     [if present]

Registry stays stable: src/collectors/__init__.py does
`from .telegram import TelegramCollector` — works for package OR module as long as
the package __init__ re-exports the class. NO registry change needed.

## Two-stage method per collector (behavior-preserving)
STAGE 1 (zero-risk): `mv <name>.py <name>/__init__.py` verbatim. The package now
  behaves byte-identically. Verify import + boot.
STAGE 2 (mechanical): inside the package, split __init__.py into collector.py +
  concern modules; __init__.py shrinks to re-exports. Verify after each split.

For small single-concern collectors (beeper 694, whatsapp 759) STAGE 1 may be
all that's warranted (a 1-file package) — uniform structure without pointless
fragmentation. The big/mixed ones get full STAGE 2.

## CI gate after EVERY change
  - ast.parse OK
  - ruff check --select E9,F821 (the existing CI gate)
  - import smoke: python -c "from src.collectors import get_collector, list_sources; [get_collector(s) for s in list_sources()]"
  - for the running collector: docker cp changed files + restart (or rebuild), watch one cycle
  - one collector per commit; push; then next

## Order (easiest -> hardest, so the harness/pattern is proven before the giants)
  1. beeper      (694)  STAGE 1 only
  2. whatsapp    (759)  STAGE 1 only
  3. search      (1015) STAGE 1 + light STAGE 2
  4. tiktok      (1132)
  5. github      (1136)
  6. website     (1182)
  7. strava      (1349)
  8. youtube     (1376)  (isolated container — extra care, restart collector_youtube)
  9. lemon8      (1665)
 10. telegram    (2617)  full STAGE 2 (parse/persist/spider/media/realtime)
 11. instagram   (2663)  full STAGE 2 (parse/persist/transport/limits/media/auth LAST)

## Rebuild discipline
Many files change -> after all packages exist, ONE image rebuild to bake, then
recreate collector + collector_youtube. Avoid the mid-build commit timing trap
(verify baked files post-build).

## Rollback
Each collector is one commit. If a cycle regresses, `git revert <sha>` that one
collector and redeploy — the others are unaffected.
