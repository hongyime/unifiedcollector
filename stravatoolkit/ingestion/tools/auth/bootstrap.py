from __future__ import annotations

import argparse

from ingestion.config import ensure_runtime_dirs, load_settings
from ingestion.session import StravaSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap and persist a Strava session cookie.")
    parser.add_argument(
        "--auth-mode",
        choices=["playwright", "cookiestxt"],
        default="playwright",
        help="How to obtain the session cookie during bootstrap.",
    )
    parser.add_argument(
        "--auth-fallback",
        choices=["auto", "playwright", "cookiestxt", "none"],
        default="auto",
        help="Fallback auth source to try if the primary source is not usable.",
    )
    parser.add_argument("--cookies-file", help="Path to Netscape-format cookies.txt file.")
    parser.add_argument("--cookie-value", help="Explicit Strava session cookie value.")
    parser.add_argument(
        "--debug-http",
        action="store_true",
        help="Print richer auth diagnostics during bootstrap.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings()
    if args.debug_http:
        settings.debug_http = True
    ensure_runtime_dirs(settings)
    session = StravaSession.from_sources(
        settings,
        auth_mode=args.auth_mode,
        auth_fallback=args.auth_fallback,
        cookie_value=args.cookie_value,
        cookies_file=args.cookies_file,
    )
    session.persist_cookie()
    if args.cookies_file and session.cookie_value:
        print(f"[auth] Session captured successfully. Active cookies file: {args.cookies_file}")
    else:
        print("[auth] Session captured successfully.")


if __name__ == "__main__":
    main()
