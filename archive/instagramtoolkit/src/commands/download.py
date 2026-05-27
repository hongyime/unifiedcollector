"""
Download command - Download media from user profiles.
"""
from src.commands.base import BaseCommand
import argparse


class DownloadCommand(BaseCommand):
    """Download media (photos, videos, stories, highlights) from profiles."""
    
    name = "download"
    description = "Download media from user profiles"
    help_text = "Download media (photos, videos, stories, highlights) from one or more users"
    
    def _add_arguments(self):
        """Add download-specific arguments."""
        self.parser.add_argument(
            'usernames',
            nargs='+',
            help='Usernames to download from'
        )
        self.parser.add_argument(
            '--post-limit',
            type=int,
            default=None,
            help='Maximum number of posts to download per user'
        )
        self.parser.add_argument(
            '--account',
            type=str,
            help='Specific Instagram account name to use'
        )
        self.parser.add_argument(
            '--operation',
            type=str,
            default='download_media',
            help='Operation type for smart routing (default: download_media)'
        )
        self.parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show account selection reasoning'
        )
    
    def execute(self, args: argparse.Namespace) -> int:
        """Execute media download using smart routing."""
        try:
            from src.commands.smart_routing_helper import run_operation
            from src.parallel_processor import InstagramProcessor

            # Build execute_fn that delegates to the existing processor
            processor = InstagramProcessor(
                account_name=args.account,
                operation_type="download"
            )

            def execute_fn(account_name: str, username: str) -> bool:
                try:
                    processor.process_batch_downloads(
                        [username],
                        post_limit=args.post_limit,
                    )
                    return True
                except Exception:
                    return False

            run_operation(
                operation_name=args.operation,
                target_usernames=args.usernames,
                execute_fn=execute_fn,
                available_accounts=[args.account] if args.account else None,
                verbose=getattr(args, 'verbose', False),
            )
            return 0

        except Exception as e:
            self.print_error(f"Download failed: {e}")
            return 1


__all__ = ["DownloadCommand"]


