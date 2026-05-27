"""Interactive Telegram session bootstrap — host-side, single account.

Replacement for the legacy ``services/login_bot`` (a Telegram bot that
accepted /startcollector commands in a chat). That whole bot-command flow has
been dropped; this is plain CLI interactive prompts using Telethon directly.

Why this exists separately from ``tools/telegram_relogin.py``:
  * ``telegram_relogin.py`` walks every TELEGRAM_ACCOUNT_<N>_* in .env and
    uses Telethon's high-level ``client.start(phone=...)`` which prompts for
    the SMS code via stdin internally.
  * ``telegram_login.py`` (this file) is for first-time bootstrap of a SINGLE
    account where you may not yet have an .env entry — you supply the phone
    interactively, manually drive ``send_code_request`` → ``sign_in``, and
    write the session to the location the collector reads.

Usage (from C:\\unifiedcollector):
    python tools/telegram_login.py
    python tools/telegram_login.py --session-name myaccount
    python tools/telegram_login.py --phone +6591234567

Reads TELEGRAM_API_ID + TELEGRAM_API_HASH from .env (or environment).
Writes the session to ``sessions/<session-name>.session`` — the same dir
the collector loads from (see src/collectors/telegram.py: session_dir =
Path("sessions")).

Idempotent: if the target session already exists and is authorised, prints
'already authenticated' and exits 0.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
SESSIONS_DIR = ROOT / "sessions"


# ---------------------------------------------------------------------------
# .env loader (no external deps — same lightweight reader the relogin tool uses)
# ---------------------------------------------------------------------------


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV.exists():
        return out
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_api_credentials(env: dict[str, str]) -> tuple[int, str]:
    """Resolve TELEGRAM_API_ID / TELEGRAM_API_HASH from env or .env file.

    Falls back to TELEGRAM_ACCOUNT_1_API_ID/_HASH so this tool works against
    the existing multi-account .env layout used by the collector.
    """
    api_id = (
        os.getenv("TELEGRAM_API_ID")
        or env.get("TELEGRAM_API_ID")
        or env.get("TELEGRAM_ACCOUNT_1_API_ID")
        or ""
    )
    api_hash = (
        os.getenv("TELEGRAM_API_HASH")
        or env.get("TELEGRAM_API_HASH")
        or env.get("TELEGRAM_ACCOUNT_1_API_HASH")
        or ""
    )
    if not api_id or not api_hash:
        sys.exit(
            "ERROR: TELEGRAM_API_ID / TELEGRAM_API_HASH not set (checked env "
            "and .env, plus TELEGRAM_ACCOUNT_1_API_ID/HASH fallback)."
        )
    try:
        api_id_int = int(api_id)
    except ValueError:
        sys.exit(f"ERROR: TELEGRAM_API_ID is not an integer: {api_id!r}")
    return api_id_int, api_hash


# ---------------------------------------------------------------------------
# Phone helpers (ported from legacy login_bot)
# ---------------------------------------------------------------------------


def sanitise_phone(raw: str) -> str:
    return re.sub(r"[ \-()\t]", "", raw)


def validate_phone(clean: str) -> bool:
    if not clean.startswith("+"):
        return False
    rest = clean[1:]
    return rest.isdigit() and len(clean) >= 7


def session_stem_from_phone(phone: str) -> str:
    return phone.lstrip("+")


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------


async def login_async(
    api_id: int,
    api_hash: str,
    session_path: Path,
    phone: str | None = None,
) -> int:
    try:
        from telethon import TelegramClient
        from telethon.errors import (
            SessionPasswordNeededError,
            PhoneCodeInvalidError,
            PhoneCodeExpiredError,
            PasswordHashInvalidError,
            FloodWaitError,
        )
    except ImportError:
        sys.exit("ERROR: telethon not installed. pip install telethon")

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Telethon takes the path WITHOUT .session
    session_no_ext = str(session_path)
    if session_no_ext.endswith(".session"):
        session_no_ext = session_no_ext[: -len(".session")]

    client = TelegramClient(session_no_ext, api_id, api_hash)

    print(f"Connecting Telegram (session={session_no_ext}.session) ...")
    await client.connect()

    # Idempotency check
    if await client.is_user_authorized():
        try:
            me = await client.get_me()
            who = "@" + (me.username or "") if getattr(me, "username", None) else (
                getattr(me, "first_name", None) or "?"
            )
            print(f"Already authenticated as {who} (+{getattr(me, 'phone', '?')}).")
            print(f"Session file: {session_no_ext}.session")
        finally:
            await client.disconnect()
        return 0

    # Phone number
    if not phone:
        phone = input("Phone number with country code (e.g. +6591234567): ").strip()
    phone = sanitise_phone(phone)
    if not validate_phone(phone):
        await client.disconnect()
        sys.exit(f"ERROR: invalid phone format: {phone!r}")

    # send_code_request
    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        await client.disconnect()
        sys.exit(f"ERROR: rate limited — retry in {e.seconds}s")
    except Exception as exc:
        await client.disconnect()
        sys.exit(f"ERROR: send_code_request failed: {exc}")

    # Code prompt loop
    me_user = None
    for attempt in range(3):
        code = input("Verification code from Telegram: ").strip()
        digits = re.sub(r"\D", "", code)[:5]
        try:
            me_user = await client.sign_in(
                phone, digits, phone_code_hash=sent.phone_code_hash
            )
            break
        except PhoneCodeInvalidError:
            print(f"  Invalid code (attempt {attempt + 1}/3). Try again.")
            continue
        except PhoneCodeExpiredError:
            await client.disconnect()
            sys.exit("ERROR: code expired. Re-run the tool.")
        except SessionPasswordNeededError:
            # 2FA path — prompt for password
            for pw_attempt in range(3):
                try:
                    import getpass
                    password = getpass.getpass("2FA password: ")
                except Exception:
                    password = input("2FA password: ")
                try:
                    me_user = await client.sign_in(password=password)
                    break
                except PasswordHashInvalidError:
                    print(f"  Wrong password (attempt {pw_attempt + 1}/3).")
                    continue
                except Exception as exc:
                    await client.disconnect()
                    sys.exit(f"ERROR: 2FA sign_in failed: {exc}")
            break
        except Exception as exc:
            await client.disconnect()
            sys.exit(f"ERROR: sign_in failed: {exc}")

    if me_user is None:
        await client.disconnect()
        sys.exit("ERROR: failed to authenticate after 3 attempts.")

    print(
        f"SUCCESS: logged in as @{getattr(me_user, 'username', None) or getattr(me_user, 'first_name', None)} "
        f"(+{getattr(me_user, 'phone', '?')})"
    )
    print(f"Session file: {session_no_ext}.session")
    print()
    print("Next steps:")
    print(f"  1. Add to .env (next free TELEGRAM_ACCOUNT_<N>_*) pointing SESSION at:")
    print(f"     sessions/{Path(session_no_ext).name}.session")
    print("  2. docker compose -f docker/docker-compose.yml restart collector")

    await client.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument(
        "--session-name",
        default=None,
        help="Filename stem under sessions/ (default: derived from phone digits).",
    )
    p.add_argument(
        "--phone",
        default=None,
        help="Phone number with country code; if omitted, prompted interactively.",
    )
    args = p.parse_args()

    env = load_env()
    api_id, api_hash = resolve_api_credentials(env)

    # Resolve session filename
    if args.session_name:
        stem = args.session_name
        if stem.endswith(".session"):
            stem = stem[: -len(".session")]
    elif args.phone:
        stem = session_stem_from_phone(sanitise_phone(args.phone))
    else:
        # No session-name and no phone → ask up front so we can derive the stem.
        prompt_phone = input("Phone number with country code (e.g. +6591234567): ").strip()
        clean = sanitise_phone(prompt_phone)
        if not validate_phone(clean):
            sys.exit(f"ERROR: invalid phone format: {clean!r}")
        args.phone = clean
        stem = session_stem_from_phone(clean)

    session_path = SESSIONS_DIR / f"{stem}.session"
    return asyncio.run(login_async(api_id, api_hash, session_path, args.phone))


if __name__ == "__main__":
    raise SystemExit(main())
