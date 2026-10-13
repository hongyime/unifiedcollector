# Legacy Matrix collector prototype

This file documents an earlier read-only Matrix `/sync` proof of concept. That
prototype is not the active Beeper collection path, and its old setup steps and
`MATRIX_COLLECTOR_ENABLED` flag no longer describe the current collector.

Current Matrix-connected collection runs through the registered
`BeeperCollector` in [`beeper/`](beeper/), which talks to Beeper Desktop's local
API and persists to the `beeper_shadow_*` tables. The code-backed platform map
and its boundaries are in [`docs/architecture.md`](../../docs/architecture.md).

The old `matrix_sync_state` migration remains in the migration history; this
prototype document should not be used to configure the active Beeper service.
