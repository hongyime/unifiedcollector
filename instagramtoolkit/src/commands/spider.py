"""
Spider command - Collect followers/following data.
"""
from src.commands.base import BaseCommand
import argparse


class SpiderCommand(BaseCommand):
    """Collect followers and following data for user profiles."""
    
    name = "spider"
    description = "Collect followers and following data"
    help_text = "Collect followers and following data for one or more users"
    
    def _add_arguments(self):
        """Add spider-specific arguments."""
        self.parser.add_argument(
            'usernames',
            nargs='*',
            help='Usernames to spider (if none, reads from data/usernames.txt)'
        )
        self.parser.add_argument(
            '--max-followers',
            type=int,
            default=1000,
            help='Maximum followers to collect per user (default: 1000)'
        )
        self.parser.add_argument(
            '--max-following',
            type=int,
            default=1000,
            help='Maximum following to collect per user (default: 1000)'
        )
        self.parser.add_argument(
            '--account',
            type=str,
            help='Specific Instagram account name to use'
        )
    
    def execute(self, args: argparse.Namespace) -> int:
        """Execute spider collection."""
        try:
            from src.parallel_processor import InstagramProcessor
            
            # Determine usernames
            if args.usernames:
                usernames = args.usernames
            else:
                from src.config import USERNAMES_FILE
                usernames = self._load_usernames(USERNAMES_FILE)
                if not usernames:
                    self.print_error("No usernames provided and file not found")
                    return 1
            
            # Create processor
            processor = InstagramProcessor(
                account_name=args.account,
                operation_type="spider"
            )
            
            # Run spider
            processor.process_batch_relationships(
                usernames,
                max_followers=args.max_followers,
                max_following=args.max_following
            )
            
            return 0
            
        except Exception as e:
            self.print_error(f"Spider failed: {e}")
            return 1
    
    def _load_usernames(self, filepath: str) -> list:
        """Load usernames from file."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []


