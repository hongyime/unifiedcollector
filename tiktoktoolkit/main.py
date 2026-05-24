"""Compatibility wrapper for the Unified TikTok Toolkit CLI."""

import sys
import signal

# Force UTF-8 output on Windows to prevent emoji/unicode crashes on cp1252 consoles
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from src.cli import cli
from src import resilience


def _signal_handler(signum, frame):
    """Handle Ctrl+C (SIGINT) — set shutdown flag, let main loop exit cleanly."""
    print("\n[Ctrl+C] Shutdown requested. Finishing current work...", file=sys.stderr)
    resilience.signal_shutdown()


def _hard_shutdown_handler(signum, frame):
    """Handle SIGTERM/SIGBREAK — set shutdown flag then exit so atexit runs."""
    print(f"\n[Signal {signum}] Shutdown requested. Closing DB...", file=sys.stderr)
    resilience.signal_shutdown()
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _hard_shutdown_handler)
if hasattr(signal, 'SIGBREAK'):  # Windows: bat window close button
    signal.signal(signal.SIGBREAK, _hard_shutdown_handler)


if __name__ == '__main__':
    cli()
