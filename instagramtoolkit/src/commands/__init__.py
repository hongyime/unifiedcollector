"""
Command registry - Available CLI commands.
"""
from src.commands.base import BaseCommand
from src.commands.spider import SpiderCommand
from src.commands.download import DownloadCommand
from src.commands.following_download import FollowingDownloadCommand
from src.commands.username_db_commands import (
    UsernameDbListCommand,
    UsernameDbMigrateCommand,
    UsernameDbExportCommand,
    UsernameDbStatsCommand,
)


def get_commands() -> dict[str, type[BaseCommand]]:
    """Get all available commands.
    
    Returns:
        dict: Mapping of command name to command class
    """
    return {
        'spider': SpiderCommand,
        'download': DownloadCommand,
        'following-download': FollowingDownloadCommand,
        'username-db-list': UsernameDbListCommand,
        'username-db-migrate': UsernameDbMigrateCommand,
        'username-db-export': UsernameDbExportCommand,
        'username-db-stats': UsernameDbStatsCommand,
    }


__all__ = ["get_commands"]


