import argparse


def test_handoff_freshness_command_registers():
    from src.cli.commands import handoff_freshness

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    handoff_freshness.register(subparsers)
    args = parser.parse_args(["handoff-freshness"])

    assert args.state_file == ".agents/STATE.md"
    assert handoff_freshness.HANDLERS[args.command]
