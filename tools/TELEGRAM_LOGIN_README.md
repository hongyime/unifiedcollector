# tools/telegram_login.py — Operator Login Guide

Single-account interactive Telegram session bootstrap. Replaces the legacy
`services/login_bot` (a Telegram bot that accepted `/startcollector` commands
in a chat — that whole bot-command UX has been dropped from the unified
collector).

## When to use which tool

| Tool | Use case |
| --- | --- |
| `tools/telegram_login.py` | First-time bootstrap of a single account. You drive each step (phone → SMS code → 2FA) manually. |
| `tools/telegram_relogin.py` | Refresh expired sessions for accounts already configured under `TELEGRAM_ACCOUNT_<N>_*` in `.env`. Walks every account in one batch. |

## Prerequisites

1. Python 3.11+ on the host with `telethon` installed (`pip install telethon`).
2. `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` available either:
   - in your shell environment, OR
   - in `C:\unifiedcollector\.env` as `TELEGRAM_API_ID=...` / `TELEGRAM_API_HASH=...`, OR
   - as `TELEGRAM_ACCOUNT_1_API_ID` / `TELEGRAM_ACCOUNT_1_API_HASH` (fallback —
     pick from any existing account row).

Run from the host (NOT inside the collector container) — Telethon needs stdin
for the SMS code prompt.

## Usage

From `C:\unifiedcollector`:

    # Fully interactive — prompts for phone, then code, then 2FA if enabled.
    python tools/telegram_login.py

    # Pre-supply phone (still prompts for the SMS code interactively).
    python tools/telegram_login.py --phone +6591234567

    # Override the session filename stem (default = phone digits).
    python tools/telegram_login.py --session-name observer1

The tool writes:

    C:\unifiedcollector\sessions\<stem>.session

…which is the exact path the unified collector reads on startup
(`session_dir = Path("sessions")` in `src/collectors/telegram.py`). No rebuild
is needed — the directory is bind-mounted into the collector container.

## Flow

1. Loads `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`.
2. Connects to Telegram.
3. **Idempotency check**: if `sessions/<stem>.session` already exists and is
   authorised, prints `Already authenticated as @...` and exits 0 with no
   prompts. Safe to re-run.
4. Otherwise prompts for phone (E.164 format), validates it, calls
   `send_code_request`.
5. Prompts for SMS code (3 attempts on `PhoneCodeInvalidError`).
6. If the account has 2FA, falls into the password prompt (3 attempts via
   `getpass`, falls back to plain `input()` on terminals without TTY).
7. On success prints the resolved username + phone and the session file path.

## After login

Add (or update) the account block in `.env`:

    TELEGRAM_ACCOUNT_2_NAME=observer1
    TELEGRAM_ACCOUNT_2_API_ID=12345678
    TELEGRAM_ACCOUNT_2_API_HASH=abcdef0123...
    TELEGRAM_ACCOUNT_2_PHONE=+6591234567
    TELEGRAM_ACCOUNT_2_SESSION=sessions/6591234567.session

…then:

    docker compose -f docker/docker-compose.yml restart collector

The collector picks up the new session automatically via `AccountPool`.

## Troubleshooting

- **`ERROR: rate limited — retry in Ns`**: Telegram's flood control hit your
  phone or API key. Wait the printed number of seconds, then retry.
- **`ERROR: code expired`**: SMS code is older than ~3 minutes; re-run the tool.
- **`already authenticated` but the collector still complains**: the on-disk
  session is fine but `.env` may point at a different filename. Either rename
  the file or update `TELEGRAM_ACCOUNT_<N>_SESSION` to match.
- **2FA password keeps failing**: this is your Telegram account's 2-step
  verification password (set in the official client under
  Settings → Privacy and Security → Two-Step Verification), NOT the SMS code.
