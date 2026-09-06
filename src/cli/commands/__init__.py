"""Grouped subcommand modules for unifiedcollector's CLI.

Each module in this package defines one or more argparse subcommands and
exposes ``register(subparsers)`` + a ``HANDLERS`` dict of ``{name: handler}``.
See :mod:`src.cli` for the aggregation logic.
"""
