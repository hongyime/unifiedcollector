import argparse

from src.cli.commands import verified_exclusion


def test_verified_exclusion_command_requires_explicit_purge_flag():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    verified_exclusion.register(subparsers)

    args = parser.parse_args(["verified-exclusion", "--manifest", "manifest-id"])

    assert args.purge is False
    assert args.cleanup_media is False
    assert args.cleanup_receipt is None
