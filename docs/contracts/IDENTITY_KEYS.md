# Identity-Key Contract (collector → analyzer)

Stable contract for how `unifiedanalyzer` resolves people from collector data.
The analyzer's `entity_platform_links.platform_id` is keyed **per source** on the
columns below. Changing a source's identity key is a breaking change — it
silently orphans every existing entity link for that source. Coordinate with
`unifiedanalyzer/src/pipeline/entity_resolver.py::load_platform_profiles` and
`timeline_builder.py` before altering any row here.

## Per-source identity key

| source (link) | collector table | `platform_id` column | shape | username/handle |
|---|---|---|---|---|
| github | `github_users` | `platform_user_id` | numeric | `login` |
| instagram | `instagram_profiles` | `platform_user_id` | numeric | `username` |
| telegram | `telegram_users` | `platform_user_id` | numeric | `username` |
| strava | `strava_athletes` | `platform_athlete_id` | numeric | `username` |
| youtube | `youtube_channels` | `platform_channel_id` | `UC…` id | `custom_url` (@handle) |
| tiktok | `tiktok_profiles` | `platform_user_id` | numeric | `username` |
| lemon8 | `lemon8_profiles` | `platform_user_id` | **⚠ vanity handle OR `userNNNN`** | `username` |
| whatsapp | `whatsapp_users` | `platform_user_id` → JID | `<phone>@s.whatsapp.net` | none (phone-keyed) |
| threads | `threads_posts` | `author_username` | handle (= IG handle) | same |
| x | `x_profiles` | `platform_user_id` | handle (same value as `x_posts.author_username`) | `username` |
| facebook | `facebook_profiles` | `platform_user_id` | handle/profile id | `username` |

### WhatsApp specifics
- A bare phone is reconstructed as `<phone>@s.whatsapp.net`.
- `@lid` group senders have NULL phone; resolve via `whatsapp_lid_map` (`lid` →
  `phone_jid`, a full JID). 15,901 rows. Used by both timeline attribution and
  the beeper bridge.

### Beeper (no own identity key)
`beeper_shadow_messages.sender_id` encodes the **native** platform id
(`@telegram_<id>`, `@instagram(go)_<id>`, `@whatsapp_<phone>`,
`@whatsapp_lid-<id>`). Beeper is bridged onto the **native source** links above
by `unifiedanalyzer/src/pipeline/beeper_bridge.py`, never a `source='beeper'`
link. Network comes from `beeper_shadow_chats.network` (also backfilled onto
`beeper_shadow_messages.network`). `@telegram_channel-*` senders are skipped.

## Cross-source bridges the analyzer relies on
- `social_users` — broad cross-platform user index; usernames on ≥2 platforms are
  corroboration fuel for clustering.
- `whatsapp_lid_map` — @lid → phone JID.
- `telegram_users.phone` ↔ `whatsapp_users` phone — same-person bridge
  (`phone_match` signal).
- `instagram_profiles.external_url` — off-platform presence (`shared_website`).

## Telegram bots
`telegram_users.is_bot` (nullable boolean, added by migration
`add_telegram_is_bot.sql`) marks bots — set from Telethon `User.bot` on every
upsert, and backfilled for the 283 `%bot`-suffixed rows (Telegram enforces bot
usernames end in `bot`). The analyzer **excludes `is_bot` from entity creation**
(`entity_resolver.load_platform_profiles`) because a shared bot contact is false
identity evidence. Channels appearing as users are not separately flagged yet
(no reliable source signal in `telegram_users`). (SYNC #40)

## Known gaps (do not silently "fix" without reading the linked task)
- **lemon8 `platform_user_id` is the vanity handle for most profiles — and that
  is the best obtainable key.** lemon8 has **no web login (mobile-app only)**, so
  the collector can only public-scrape logged-out pages, which do NOT expose a
  stable numeric id. `userNNNN` appears only for accounts that never set a vanity
  handle (~14 today). The collector prefers a stable id when the page exposes one
  and renames a vanity row in place if it ever does (see `_upsert_profile`,
  SYNC #39), but bulk retro-conversion is impossible without the mobile API.
  Treat the vanity handle as the canonical lemon8 key. **Do not re-attempt a
  cookie/login-based backfill — there is no web login.**
- **gone media not tombstoned.** `media_items` rows can point at files deleted
  from disk (e.g. lemon8 after the Z reformat); there is no `status` column, so
  consumers must tolerate missing files. (SYNC #38 / analyzer SYNC #36)
