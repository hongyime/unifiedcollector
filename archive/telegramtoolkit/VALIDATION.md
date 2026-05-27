# Telegram Toolkit — Validation Status

**Last validated:** 2026-05-13 (updated same day)
**Test result:** 202/202 passing

## What the toolkit does

Multi-account Telegram OSINT/automation toolkit (Telethon/MTProto). Scans groups for links, users, and media simultaneously. Joins/leaves groups in bulk. Downloads profile photos. Sends photos to chats. Backs up and resends deleted messages. Serves a local web dashboard and network visualizer.

## Verified working

| Feature | Entry | Status |
|---------|-------|--------|
| Unified scan (links + users + media) | Menu 1 / `main.py unified` | ✅ |
| Join groups | Menu 2 / `main.py join` | ✅ |
| Leave groups | Menu 3 / `main.py leave` | ✅ |
| Download media | Menu 4 / `main.py media` | ✅ |
| User analysis | Menu 5 / `main.py users` | ✅ |
| Collect Telegram links | Menu 6 / `main.py links` | ✅ |
| Multi-platform links | Menu 7 / `main.py multi` | ✅ |
| Profile photo download | Menu 8 / `main.py profiles` | ✅ |
| Send photos | Menu 9 / `main.py photos` | ✅ |
| Dashboard (HTTP server) | Menu 10 / `start_toolkit.bat dashboard` | ✅ |
| Network visualizer | Menu 11 / `start_toolkit.bat visualize` | ✅ |
| Data export (JSON/Excel/report) | Menu 12 / `main.py export` | ✅ |
| Account manager (add/list/remove) | Menu 13 / `main.py accounts` | ✅ (fixed) |
| Tracking / reset state | Menu 14 / `main.py state` | ✅ |
| Backup deleted messages | Menu 15 / `main.py backup` | ✅ |
| Resend backed-up messages | Menu 16 / `main.py resend` | ✅ |
| Full pipeline | `main.py pipeline` | ✅ |
| HTTP server only | Menu 18 in quick_actions.bat | ✅ (fixed) |

## Database state (2026-05-13)

- Users: 36,812
- Memberships: 44,495
- Download hashes: 223,580
- Scan checkpoints: 925
- Feature checkpoints: 1,062
- Schema version: 5

## Fixes applied

See `memory/fixes_applied.md` for full details. Summary:

1. `start_toolkit.bat` / `quick_actions.bat` — wrong `toolkit.server.simple_server` → `src.server.simple_server`
2. `AccountManager` — rewrote load/save to use `.env` instead of broken config.py regex
3. `DynamicConfig.config_file` — wrong path `toolkit/core/config.py` → `src/core/config.py`
4. `StateManager._create_tables()` — index creation crash on pre-v4 databases
5. `StateManager.is_failed_lookup()` — inverted `<` operator → `>` (7 affected tests)
6. All test files — `patch("toolkit.managers....")` → `patch("src.managers....")` (19 failures fixed)
7. `test_ingress_inventory.py` — hardcoded `toolkit/` paths → `src/`
8. `test_e2e_menu_routing.py` — stale "Invalid choice" assertion
9. `test_failed_username_handling.py` — wrong expected summary count (3 → 4)

## Additional fixes (same session)

10. `collected_links.txt` (46,161 links) imported → SQLite `link_collection`, file deleted
11. `Users.csv` (stale 21K) deleted — SQLite is authoritative (36,812 users)
12. Empty legacy sidecars deleted: `downloaded_hashes.json`, `downloaded_profile_photos.json`, `photo_send_progress.json`, `sent_photo_hashes.json`
13. `show_stats()` reads SQLite directly (links, users, memberships) — no more CSV file reads
14. `AccountManager.add_new_account()` injects new `ACCOUNT_N_*` vars into `os.environ` live
15. `AccountManager.remove_account()` clears + re-compacts all `ACCOUNT_N_*` env vars live
