"""
Telegram Toolkit
A comprehensive Telegram monitoring and data extraction toolkit.

Usage:
    from src import *  # Not recommended - specific imports preferred
    from src.core import state_manager
    from src.managers import join_groups
"""

from src.core.console import configure_console_output

# Public API exports
__all__ = [
    # Core Package
    "core",
    
    # Managers Package
    "managers",
    
    # Server Package
    "server",
]

# Initialize console output on import
configure_console_output()
