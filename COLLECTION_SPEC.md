# Collection Specification — Capture Standard for All Collectors

> **Status:** authoritative. Every collector implements this standard; each adapts it to
> what its platform actually exposes (see the applicability matrix). **When adding a NEW
> source, implement this spec and add a row to the matrix.** Defined by the project owner
> 2026-06-13. Future LLM agents: treat this as requirements, not suggestions.

## Capture priority (process in this order when the ban/IP budget is tight)

1. **Ephemeral** — expires in ~24h: Instagram/Telegram/TikTok Stories, WhatsApp Status.
2. **Media** — photos, videos, message bubbles (text).
3. **Documents & audio**.
4. **Profile content** — bio, profile photo, creation date, memberships, comments, reactions.
5. **Location** — GPS, shared/live location, EXIF.
6. **Everything else** — links, polls, pinned, events.

Scheduling MUST honour this tier order (ephemeral first).

---

## Locked requirements (apply per collector)

### Tier 1 — Ephemeral / Stories
- Poll **only stories visible to our logged-in accounts** (use the follow-aware multi-account
  access model: pick an account that can see the target; private targets only if an account follows them).
- Poll cadence: **every 4 hours** per target.
- **Reuse the same collection accounts** (no dedicated/isolated story accounts).
- **All platforms in v1** (those that have stories: instagram, telegram, tiktok, whatsapp-status).
- **Storage:** stories are media → rows in the shared `media_items` table with
  `content_type='story'` / `'story_video'`, tagged by `source`, file on disk. **No separate schema.**

### Tier 2 — Media
- **Highest quality always** (HD photos, best video stream).
- **Download full** — no size cap (download large videos in full).
- Edits/deletes: **track edits + mark deletions — DEFERRED** (not now; first get all content into DB).

### Tier 3 — Documents & audio
- Documents: **whitelist safe types only** — PDF, Word, PowerPoint, Office, images, text.
  **Skip executables and code files.**
- Audio: **store the file** (no transcription, except YouTube where transcripts are already available).
- Stickers: **collect static, skip animated** (.tgs/.webm).

### Tier 4 — Profile content
- **Full change history** — bio history, username changes, profile-pic history (pHash-based).
- Collect **comments AND reactions** (post comments, message/post reactions).
- Collect **memberships** (groups/channels joined) **and spider the follower/following graph** for discovery.

### Tier 5 — Location
- **Extract EXIF GPS** from downloaded photos/videos (best-effort; social platforms often strip metadata).
- **Parse shared/live-location messages** (telegram/whatsapp) into structured coordinates.

### Tier 6 — Everything else
- **Extract & resolve links** from message text / bios / descriptions → feed the spider (cross-platform discovery).
- Collect **polls, pinned messages, and events/RSVP**.

### Cross-cutting
- **Backfill depth: everything available** (full message/post history, all media).
- **Tier-ordered scheduling** (see priority above).
- Dedup via sha256 (existing `media_items` pattern); files on disk, metadata in Postgres.

---

## Per-collector applicability matrix

Legend: ✅ supported/required · ➖ N/A for platform · 🔲 to build

| Tier / feature | instagram | telegram | tiktok | youtube | strava | lemon8 | website | github | whatsapp | beeper |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 Stories/ephemeral | ✅ | ✅ | 🔲 | ➖ | ➖ | ➖ | ➖ | ➖ | ✅(status) | ➖ |
| 2 Media (photo/video) | ✅ | ✅ | ✅ | ✅ | ✅(activity) | ✅ | ✅ | ➖(avatars) | ✅ | ✅ |
| 2 Message bubbles | ➖ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ✅ | ✅ |
| 3 Documents | ➖ | ✅ | ➖ | ➖ | ➖ | ➖ | ✅(pdf) | ➖ | ✅ | ✅ |
| 3 Audio (store) | ➖ | ✅ | ➖ | ✅(file) | ➖ | ➖ | ➖ | ➖ | ✅ | ✅ |
| 4 Profile + change history | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ➖ | ✅ | ✅ | ✅ |
| 4 Comments/reactions | ✅ | ✅ | ✅ | ✅ | ✅(kudos/comments) | ✅ | ➖ | ➖ | ✅ | ✅ |
| 4 Memberships + graph spider | ✅ | ✅ | ✅ | ✅(subs) | ✅(follows) | ✅ | ➖ | ✅(contrib) | ✅ | ✅ |
| 5 Platform geotags | ✅ | ✅ | ✅ | ➖ | ✅(GPS) | ✅ | ➖ | ➖ | ✅(loc msg) | ✅ |
| 5 EXIF GPS from media | ✅ | ✅ | ✅ | ➖ | ➖ | ✅ | ✅ | ➖ | ✅ | ✅ |
| 6 Links | ✅ | ✅ | ✅ | ✅(desc) | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 6 Polls/pinned/events | ➖ | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ✅ | ✅ |

_(Adjust cells as platform capabilities are confirmed during implementation.)_

---

## Storage conventions
- **All media** → shared `media_items` table (`source`, `entity_id`, `content_type`, `content_id`,
  `filename`, `file_path`, `file_size`, `sha256`, `metadata` jsonb, `collected_at`). Files on the media volume.
- `content_type` values include: `image`, `video`, `photo`, `story`, `story_video`, `profile_photo`,
  `document`, `audio`, `sticker`, `thumbnail`, `activity_photo`, `pdf`, `post`.
- Structured (non-media) data → per-platform tables (`<source>_posts`, `<source>_messages`,
  `<source>_profiles`, reactions/polls/relationships, etc.).
- `metadata` jsonb columns: **always `json.dumps()`** the value (asyncpg has no dict→jsonb codec here —
  passing a raw dict silently fails the insert).

## Adding a new source
1. Implement each tier the platform supports (use the matrix; fill a new column).
2. Reuse `media_items` for all media (incl. stories) and `BaseCollector` patterns (dedup, backfill, media_download).
3. Honour the follow-aware account model and tier-ordered scheduling.
4. Update this spec's matrix.
