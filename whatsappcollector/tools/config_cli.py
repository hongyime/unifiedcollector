#!/usr/bin/env python3
"""
tools/config_cli.py — CLI for inspecting and mutating live config values.

Usage:
    python tools/config_cli.py list [--service SERVICE]
    python tools/config_cli.py get <service> <key>
    python tools/config_cli.py set <service> <key> <value>
    python tools/config_cli.py reset <service> <key>
    python tools/config_cli.py reset-all <service>
"""

from __future__ import annotations

import argparse
import os
import sys

# ---------------------------------------------------------------------------
# Load REDIS_URL from .env (python-dotenv optional, falls back to os.environ)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; rely on os.environ

# ---------------------------------------------------------------------------
# Shared imports
# ---------------------------------------------------------------------------
# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.live_config import (  # noqa: E402
    PARAMETER_REGISTRY,
    ConfigValidationError,
    ParameterMeta,
)

import redis as redis_lib  # noqa: E402  (sync client for CLI)

# ---------------------------------------------------------------------------
# Rich table support (optional)
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.table import Table

    _console = Console()
    _RICH = True
except ImportError:
    _RICH = False

# ---------------------------------------------------------------------------
# Redis connection
# ---------------------------------------------------------------------------

def _get_redis() -> redis_lib.Redis:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis_lib.Redis.from_url(redis_url, decode_responses=True)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_service(service: str) -> None:
    """Exit 1 if service is not in PARAMETER_REGISTRY."""
    if service not in PARAMETER_REGISTRY:
        valid = ", ".join(sorted(PARAMETER_REGISTRY.keys()))
        print(f"Error: unknown service {service!r}. Valid services: {valid}", file=sys.stderr)
        sys.exit(1)


def validate_key(service: str, key: str) -> ParameterMeta:
    """Exit 1 if key is not registered for service. Returns the ParameterMeta."""
    registry = {m.key: m for m in PARAMETER_REGISTRY[service]}
    if key not in registry:
        valid = ", ".join(sorted(registry.keys()))
        print(
            f"Error: unknown parameter {key!r} for service {service!r}.\n"
            f"Valid keys: {valid}",
            file=sys.stderr,
        )
        sys.exit(1)
    return registry[key]


def coerce_value(meta: ParameterMeta, raw_value: str):
    """
    Coerce raw_value to meta.python_type and validate range/options.
    Raises ConfigValidationError on failure (same logic as push()).
    """
    if meta.python_type is bool:
        if raw_value.lower() in ("true", "1", "yes"):
            coerced = True
        elif raw_value.lower() in ("false", "0", "no"):
            coerced = False
        else:
            raise ConfigValidationError(
                f"Invalid bool value {raw_value!r} for {meta.key!r}; "
                "expected one of: true, false, 1, 0, yes, no"
            )
    elif meta.python_type is int:
        try:
            coerced = int(raw_value)
        except ValueError:
            raise ConfigValidationError(
                f"Invalid int value {raw_value!r} for {meta.key!r}"
            )
    elif meta.python_type is float:
        try:
            coerced = float(raw_value)
        except ValueError:
            raise ConfigValidationError(
                f"Invalid float value {raw_value!r} for {meta.key!r}"
            )
    else:
        coerced = raw_value  # str — use as-is

    # Range check
    if meta.min_value is not None and coerced < meta.min_value:
        raise ConfigValidationError(
            f"Value {coerced!r} for {meta.key!r} is below minimum {meta.min_value}"
        )
    if meta.max_value is not None and coerced > meta.max_value:
        raise ConfigValidationError(
            f"Value {coerced!r} for {meta.key!r} exceeds maximum {meta.max_value}"
        )

    # Options check
    if meta.options is not None and str(coerced) not in meta.options:
        raise ConfigValidationError(
            f"Value {coerced!r} for {meta.key!r} is not in allowed options: {meta.options}"
        )

    return coerced

# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    """list [--service SERVICE]"""
    r = _get_redis()

    if args.service:
        validate_service(args.service)
        services_to_show = [args.service]
    else:
        services_to_show = sorted(PARAMETER_REGISTRY.keys())

    for service in services_to_show:
        params = PARAMETER_REGISTRY[service]
        live_hash = r.hgetall(f"live_config:{service}")

        if _RICH:
            table = Table(title=f"[bold]{service}[/bold]", show_lines=False)
            table.add_column("Parameter", style="cyan", no_wrap=True)
            table.add_column("Type", style="dim")
            table.add_column("Default")
            table.add_column("Live Value")
            table.add_column("Description")

            for meta in params:
                default_str = str(meta.default)
                raw_live = live_hash.get(meta.key)
                if raw_live is not None:
                    differs = raw_live != default_str
                    live_str = f"[yellow]{raw_live} ★[/yellow]" if differs else raw_live
                else:
                    live_str = "[dim]not set[/dim]"

                table.add_row(
                    meta.key,
                    meta.python_type.__name__,
                    default_str,
                    live_str,
                    meta.description,
                )

            _console.print(table)
        else:
            # Plain tab-separated fallback
            print(f"\n=== {service} ===")
            header = "\t".join(["Parameter", "Type", "Default", "Live Value", "Description"])
            print(header)
            print("-" * len(header))
            for meta in params:
                default_str = str(meta.default)
                raw_live = live_hash.get(meta.key)
                if raw_live is not None:
                    differs = raw_live != default_str
                    live_str = f"{raw_live} ★" if differs else raw_live
                else:
                    live_str = "not set"
                print(f"{meta.key}\t{meta.python_type.__name__}\t{default_str}\t{live_str}\t{meta.description}")

    if not _RICH:
        print("\n★ = differs from .env default")


def cmd_get(args: argparse.Namespace) -> None:
    """get <service> <key>"""
    validate_service(args.service)
    validate_key(args.service, args.key)

    r = _get_redis()
    raw = r.hget(f"live_config:{args.service}", args.key)
    if raw is None:
        print("not set")
    else:
        print(raw)


def cmd_set(args: argparse.Namespace) -> None:
    """set <service> <key> <value>"""
    validate_service(args.service)
    meta = validate_key(args.service, args.key)

    try:
        coerced = coerce_value(meta, args.value)
    except ConfigValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    r = _get_redis()
    r.hset(f"live_config:{args.service}", args.key, str(coerced))
    print(f"✓ {args.service}.{args.key} = {coerced}")

    if meta.requires_restart:
        print("⚠ This parameter requires a container restart to take effect.")


def cmd_reset(args: argparse.Namespace) -> None:
    """reset <service> <key>"""
    validate_service(args.service)
    meta = validate_key(args.service, args.key)

    r = _get_redis()
    r.hdel(f"live_config:{args.service}", args.key)
    print(f"✓ {args.service}.{args.key} reset to default ({meta.default})")


def cmd_reset_all(args: argparse.Namespace) -> None:
    """reset-all <service>"""
    validate_service(args.service)

    r = _get_redis()
    r.delete(f"live_config:{args.service}")
    print(f"✓ All overrides for {args.service} cleared.")


def cmd_diff(args: argparse.Namespace) -> None:
    """diff — show parameters whose live value differs from env default."""
    r = _get_redis()
    diffs: list[tuple[str, str, str]] = []  # (label, default, live_value)

    for service in sorted(PARAMETER_REGISTRY.keys()):
        live_hash = r.hgetall(f"live_config:{service}")
        for meta in PARAMETER_REGISTRY[service]:
            raw_live = live_hash.get(meta.key)
            if raw_live is not None and raw_live != str(meta.default):
                diffs.append((f"{service}.{meta.key}", str(meta.default), raw_live))

    if not diffs:
        print("No live overrides differ from defaults.")
        return

    if _RICH:
        table = Table(title="Live Config Diffs", show_lines=False)
        table.add_column("Parameter", style="cyan", no_wrap=True)
        table.add_column("Default", style="dim")
        table.add_column("Live Value", style="yellow")
        for label, default, live_value in diffs:
            table.add_row(label, default, live_value)
        _console.print(table)
    else:
        for label, default, live_value in diffs:
            print(f"{label}: {default} → {live_value}")


def cmd_export(args: argparse.Namespace) -> None:
    """export — output .env-formatted snippet of all current live values."""
    r = _get_redis()
    any_output = False

    for service in sorted(PARAMETER_REGISTRY.keys()):
        live_hash = r.hgetall(f"live_config:{service}")
        if not live_hash:
            continue
        any_output = True
        print(f"# {service}")
        for meta in PARAMETER_REGISTRY[service]:
            if meta.key in live_hash:
                print(f"{meta.key}={live_hash[meta.key]}")
        print()

    if not any_output:
        # Print an empty comment block when nothing is set
        for service in sorted(PARAMETER_REGISTRY.keys()):
            print(f"# {service}")

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="config_cli",
        description="Inspect and mutate live config values stored in Redis.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List parameters (optionally filtered by service)")
    p_list.add_argument("--service", metavar="SERVICE", default=None,
                        help="Show only this service's parameters")
    p_list.set_defaults(func=cmd_list)

    # get
    p_get = subparsers.add_parser("get", help="Get the current live value of a parameter")
    p_get.add_argument("service", help="Service name")
    p_get.add_argument("key", help="Parameter key")
    p_get.set_defaults(func=cmd_get)

    # set
    p_set = subparsers.add_parser("set", help="Set a live config value")
    p_set.add_argument("service", help="Service name")
    p_set.add_argument("key", help="Parameter key")
    p_set.add_argument("value", help="New value (will be validated and coerced)")
    p_set.set_defaults(func=cmd_set)

    # reset
    p_reset = subparsers.add_parser("reset", help="Reset a parameter to its env default")
    p_reset.add_argument("service", help="Service name")
    p_reset.add_argument("key", help="Parameter key")
    p_reset.set_defaults(func=cmd_reset)

    # reset-all
    p_reset_all = subparsers.add_parser("reset-all", help="Clear all overrides for a service")
    p_reset_all.add_argument("service", help="Service name")
    p_reset_all.set_defaults(func=cmd_reset_all)

    # diff
    p_diff = subparsers.add_parser("diff", help="Show parameters whose live value differs from env default")
    p_diff.set_defaults(func=cmd_diff)

    # export
    p_export = subparsers.add_parser("export", help="Output .env-formatted snippet of all current live values")
    p_export.set_defaults(func=cmd_export)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
