"""Re-authorize all Telegram sessions interactively, on the host.

Run this OUTSIDE Docker because Telethon needs stdin for the SMS code and
optional 2FA password. Once each session file is written, the Docker collector
will pick it up via the bind-mounted ./sessions volume — no rebuild needed,
just `docker compose restart collector`.

Usage (from C:\\unifiedcollector):
    python tools/telegram_relogin.py            # interactive, all accounts
    python tools/telegram_relogin.py --account 1  # one specific account

Reads credentials from .env (TELEGRAM_ACCOUNT_<N>_API_ID/API_HASH/PHONE/SESSION).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV.exists():
        sys.exit(f"ERROR: {ENV} not found")
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def get_accounts(env: dict[str, str]) -> list[dict]:
    accts = []
    for n in range(1, 10):
        api_id = env.get(f"TELEGRAM_ACCOUNT_{n}_API_ID", "")
        api_hash = env.get(f"TELEGRAM_ACCOUNT_{n}_API_HASH", "")
        phone = env.get(f"TELEGRAM_ACCOUNT_{n}_PHONE", "")
        session = env.get(f"TELEGRAM_ACCOUNT_{n}_SESSION", "")
        name = env.get(f"TELEGRAM_ACCOUNT_{n}_NAME", f"account_{n}")
        if not (api_id and api_hash and phone and session):
            continue
        accts.append(
            {
                "n": n,
                "name": name,
                "api_id": int(api_id),
                "api_hash": api_hash,
                "phone": phone,
                "session": session,  # relative path like sessions/6592348112.session
            }
        )
    return accts


def relogin(acct: dict) -> bool:
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        sys.exit("ERROR: telethon not installed. Run: pip install telethon")

    # Telethon takes the session path WITHOUT the .session extension
    session_path = acct["session"]
    if session_path.endswith(".session"):
        session_path = session_path[: -len(".session")]
    session_abs = ROOT / session_path
    session_abs.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Account {acct['n']}: {acct['name']} ({acct['phone']}) ===")
    print(f"  Session file: {session_abs}.session")

    client = TelegramClient(str(session_abs), acct["api_id"], acct["api_hash"])
    try:
        client.connect()
        if client.is_user_authorized():
            me = client.get_me()
            print(f"  ALREADY AUTHORIZED as @{me.username or me.first_name} (+{me.phone})")
            client.disconnect()
            return True

        # Not authorized — start interactive login
        print("  Not authorized. Starting interactive login...")
        client.start(phone=acct["phone"])
        me = client.get_me()
        print(f"  SUCCESS: logged in as @{me.username or me.first_name} (+{me.phone})")
        client.disconnect()
        return True
    except Exception as e:
        print(f"  FAILED: {e}")
        try:
            client.disconnect()
        except Exception:
            pass
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--account", type=int, help="Only process this account number")
    args = p.parse_args()

    env = load_env()
    accts = get_accounts(env)
    if args.account:
        accts = [a for a in accts if a["n"] == args.account]
        if not accts:
            sys.exit(f"ERROR: account {args.account} not found in .env")

    print(f"Processing {len(accts)} Telegram account(s)...")
    print("For each NOT-yet-authorized account, you will be prompted for:")
    print("  1. SMS code sent to your phone (or Telegram app)")
    print("  2. 2FA password if you have one set")
    print()

    results = []
    for a in accts:
        ok = relogin(a)
        results.append((a["name"], ok))

    print("\n=== Summary ===")
    for name, ok in results:
        print(f"  {'OK' if ok else 'FAIL'}  {name}")

    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"\n{len(failed)} account(s) failed. Re-run after fixing the issue.")
        sys.exit(1)
    print("\nAll sessions authorized. Now restart the collector to pick them up:")
    print("  cd C:\\unifiedcollector")
    print("  docker compose -f docker/docker-compose.yml restart collector")


if __name__ == "__main__":
    main()
