#!/usr/bin/env python3
"""
Priority Manager - Prioritize usernames based on account relationships
"""

import os
import sys
from src.config import DATA_DIR
from src.profile_access_tracker import ProfileAccessTracker

def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class PriorityManager:
    """Manage priority ordering of usernames for batch processing"""
    
    def __init__(self):
        self.access_tracker = ProfileAccessTracker()
        self.relationships = self._load_relationships()
    
    def _load_relationships(self):
        """Load relationships from the database."""
        try:
            from db.repositories.relationship_repository import RelationshipRepository
            rows = RelationshipRepository(_get_db()).get_relationships()
            print(f"[PRIORITY] Loaded {len(rows)} relationships for prioritization")
            return rows
        except Exception as e:
            print(f"[WARNING] Error loading relationships for prioritization: {e}")
        return []
    
    def get_account_connections(self, account_username):
        """
        Get followers and following for a specific account
        
        Args:
            account_username: The username of the account to analyze
            
        Returns:
            dict: {'followers': set(), 'following': set()}
        """
        connections = {
            'followers': set(),  # People who follow this account
            'following': set()   # People this account follows
        }
        
        for rel in self.relationships:
            source = rel.get('source')
            target = rel.get('target')
            rel_type = rel.get('type')
            
            if source == account_username:
                # This account is the source
                if rel_type == 'followers':
                    # Target is a follower of this account
                    connections['followers'].add(target)
                elif rel_type == 'following':
                    # Target is someone this account follows
                    connections['following'].add(target)
            elif target == account_username:
                # This account is the target - need to reverse the relationship
                if rel_type == 'followers':
                    # Source follows this account, so source is a follower
                    connections['followers'].add(source)
                elif rel_type == 'following':
                    # Source is followed by this account, so this account follows source
                    connections['following'].add(source)
        
        print(f"[PRIORITY] Found {len(connections['followers'])} followers and {len(connections['following'])} following for {account_username}")
        return connections
    
    def prioritize_usernames(self, usernames, account_username):
        """
        Prioritize usernames based on their relationship to the account
        
        Priority order:
        1. Mutual connections (people who follow you AND you follow back)
        2. Your followers (people who follow you)
        3. People you follow
        4. Public accounts (known accessible)
        5. Unknown/private accounts
        
        Args:
            usernames: List of usernames to prioritize
            account_username: Username of the account being used
            
        Returns:
            dict: Categorized and prioritized usernames
        """
        print(f"[PRIORITY] Prioritizing {len(usernames)} usernames for account: {account_username}")
        
        # Get account connections
        connections = self.get_account_connections(account_username)
        followers = connections['followers']
        following = connections['following']
        
        # Get access data
        access_stats = self.access_tracker.get_access_statistics()
        
        # Categorize usernames
        categories = {
            'mutual_connections': [],      # Follow each other
            'followers_only': [],          # They follow you
            'following_only': [],          # You follow them
            'public_accessible': [],       # Known public accounts
            'unknown_private': []          # Unknown or private accounts
        }
        
        for username in usernames:
            if username in followers and username in following:
                categories['mutual_connections'].append(username)
            elif username in followers:
                categories['followers_only'].append(username)
            elif username in following:
                categories['following_only'].append(username)
            else:
                # Check if it's a known public account
                profile_summary = self.access_tracker.get_profile_summary(username)
                if profile_summary.get('is_public', False):
                    categories['public_accessible'].append(username)
                else:
                    categories['unknown_private'].append(username)
        
        # Print prioritization summary
        self._print_prioritization_summary(categories, account_username)
        
        return categories
    
    def get_prioritized_list(self, usernames, account_username):
        """
        Get a single prioritized list of usernames
        
        Args:
            usernames: List of usernames to prioritize
            account_username: Username of the account being used
            
        Returns:
            list: Prioritized list of usernames
        """
        categories = self.prioritize_usernames(usernames, account_username)
        
        # Combine in priority order
        prioritized = []
        prioritized.extend(categories['mutual_connections'])
        prioritized.extend(categories['followers_only'])
        prioritized.extend(categories['following_only'])
        prioritized.extend(categories['public_accessible'])
        prioritized.extend(categories['unknown_private'])
        
        return prioritized
    
    def get_high_priority_users(self, usernames, account_username, max_users=None):
        """
        Get only high-priority users (mutual, followers, following) up to a limit
        
        Args:
            usernames: List of usernames to prioritize
            account_username: Username of the account being used
            max_users: Maximum number of users to return (None for all)
            
        Returns:
            list: High-priority users only
        """
        categories = self.prioritize_usernames(usernames, account_username)
        
        # Get high priority categories only
        high_priority = []
        high_priority.extend(categories['mutual_connections'])
        high_priority.extend(categories['followers_only'])  
        high_priority.extend(categories['following_only'])
        
        if max_users and len(high_priority) > max_users:
            high_priority = high_priority[:max_users]
            print(f"[LIMIT] Limited to {max_users} high-priority users")
        
        print(f"[HIGH-PRIORITY] Selected {len(high_priority)} high-priority users from {len(usernames)} total")
        return high_priority
    
    def _print_prioritization_summary(self, categories, account_username):
        """Print a summary of the prioritization"""
        print(f"\n[PRIORITY] Prioritization Summary for {account_username}")
        print("=" * 60)
        
        total = sum(len(cat) for cat in categories.values())
        
        print(f"[HIGH] Mutual connections (follow each other): {len(categories['mutual_connections'])}")
        print(f"[HIGH] Your followers: {len(categories['followers_only'])}")
        print(f"[MED]  People you follow: {len(categories['following_only'])}")
        print(f"[MED]  Known public accounts: {len(categories['public_accessible'])}")
        print(f"[LOW]  Unknown/private accounts: {len(categories['unknown_private'])}")
        print(f"[TOTAL] Total accounts: {total}")
        
        if len(categories['mutual_connections']) + len(categories['followers_only']) + len(categories['following_only']) > 0:
            high_priority_count = len(categories['mutual_connections']) + len(categories['followers_only']) + len(categories['following_only'])
            print(f"\n[SUCCESS] {high_priority_count}/{total} ({high_priority_count/total*100:.1f}%) accounts have high/medium priority access!")
        else:
            print(f"\n[WARNING] No high-priority accounts found. Consider building relationships first.")
    
    def get_category_stats(self, usernames, account_username):
        """Get detailed statistics about username categories"""
        categories = self.prioritize_usernames(usernames, account_username)
        
        stats = {}
        for category, users in categories.items():
            stats[category] = {
                'count': len(users),
                'percentage': len(users) / len(usernames) * 100 if usernames else 0,
                'usernames': users[:10]  # First 10 as examples
            }
        
        return stats


def print_priority_analysis(usernames, account_username):
    """Print detailed priority analysis for a list of usernames"""
    manager = PriorityManager()
    
    print(f"\n[ANALYSIS] Priority Analysis for {len(usernames)} usernames")
    print(f"[ACCOUNT] Using account: {account_username}")
    
    stats = manager.get_category_stats(usernames, account_username)
    
    print("\n[DETAILS] Category Breakdown:")
    print("-" * 40)
    
    for category, data in stats.items():
        print(f"\n{category.replace('_', ' ').title()}:")
        print(f"  Count: {data['count']} ({data['percentage']:.1f}%)")
        if data['usernames']:
            print(f"  Examples: {', '.join(data['usernames'][:5])}")





