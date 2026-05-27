# Command Modules (Planned Architecture)

**Status:** Not Active — Aspirational Refactor Target

These command modules represent a planned refactoring of the monolithic CLI dispatcher in `main.py`. They are not currently wired into the application.

## Current State

- `main.py` uses argparse-based monolithic dispatcher (1000+ lines)
- Command modules exist but are not imported or used at runtime
- All CLI logic is inline in `main.py`

## Existing Command Modules

- `base.py` — Base command class interface
- `spider.py` — Relationship collection commands
- `download.py` — Media download commands
- `following_download.py` — Following-based download commands
- `username_db_commands.py` — Username database management
- `smart_routing_helper.py` — Smart routing utilities

## Future State (Planned)

- Each command will be a separate module inheriting from `BaseCommand`
- `main.py` will delegate to command modules via a dispatcher
- Cleaner separation of concerns
- Easier testing and maintenance

## References

See `main.py` lines 101-107 for TODO comment about this planned refactoring.

---

**Note:** If you're looking for the actual CLI implementation, see `main.py`. These modules are architectural placeholders for future work.
