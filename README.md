# unifiedcollector

Unified ingestion plane for 11 source platforms (github, youtube, strava, search, website, tiktok, lemon8, whatsapp, telegram, instagram, matrix). Read-only by design.

## Outbound functionality — intentionally absent

Unified collector is read-only by design. The following toolkit features were
**INTENTIONALLY DROPPED** during the Wave 2 port — they are not regressions and
should not be "restored" as missing functionality:

- **Telegram**: `shared/media_uploader.py`, `src/managers/resender.py`,
  `src/managers/send_photos.py`
- **WhatsApp**: `services/bulk_sender/`
- **Generic (across platforms)**: send / reply / react / edit / delete / typing
  indicators / mark-as-read / bot-command-handler

Rationale: **collection without contamination.** This service observes and
archives; it never writes back to the source platform. Mixing outbound
primitives into the collector creates ambiguity about whether a message in the
unified DB originated from a real user or from our own automation, and
materially raises the blast radius of any bug or credential leak.

Future maintainers: if outbound is needed for a specific use case, build it as
a **separate service that consumes the unified DB** — do not embed it in the
collector. The toolkits archived under `archive/` retain the
original outbound implementations as reference.
