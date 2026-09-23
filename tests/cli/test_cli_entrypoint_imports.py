import argparse


def test_register_all_imports_and_registers_every_command_module():
    from src.cli import register_all

    parser = argparse.ArgumentParser(description="UnifiedCollector")
    sub = parser.add_subparsers(dest="command")

    handlers = register_all(sub)

    assert handlers, "register_all() must return at least one command handler"
    assert all(callable(handler) for handler in handlers.values())
