"""Profile analyzer for generating network insights from metadata.

Analyzes profile metadata to identify influential users, network topology,
and provide actionable insights about scraped networks.
"""
import os
import csv
import time
from typing import Dict, Any, List, Optional
from src.user_metadata_manager import UserMetadataManager
from src.config import DATA_DIR
from src.io_utils import safe_json_write


class ProfileAnalyzer:
    """Analyze profile metadata for network insights.
    
    Generates statistics, identifies influential users, and provides
    actionable insights about the scraped network.
    """
    
    def __init__(self):
        self.metadata_manager = UserMetadataManager()
        self.stats_file = os.path.join(DATA_DIR, 'profile_stats.json')
        self.csv_file = os.path.join(DATA_DIR, 'profile_stats.csv')
    
    def analyze_network(self) -> Dict[str, Any]:
        """Generate comprehensive network analysis.
        
        Returns:
            Dict with complete network statistics
        """
        stats = self.metadata_manager.get_network_stats()
        
        # Add additional analysis
        profiles = list(self.metadata_manager.get_all_profiles().values())
        
        if profiles:
            # Calculate ratios
            ratios = []
            for p in profiles:
                followers = p.get('followers_count', 0)
                following = p.get('following_count', 0)
                if following > 0:
                    ratios.append(followers / following)
            
            stats['avg_follower_to_following_ratio'] = sum(ratios) / len(ratios) if ratios else 0
            
            # Count by size tiers
            tiers = {
                'micro_influencers': 0,      # 1K-10K
                'small_influencers': 0,      # 10K-50K
                'medium_influencers': 0,     # 50K-100K
                'large_influencers': 0,      # 100K-500K
                'mega_influencers': 0,       # 500K-1M
                'celebrities': 0             # 1M+
            }
            
            for p in profiles:
                f = p.get('followers_count', 0)
                if 1000 <= f < 10000:
                    tiers['micro_influencers'] += 1
                elif 10000 <= f < 50000:
                    tiers['small_influencers'] += 1
                elif 50000 <= f < 100000:
                    tiers['medium_influencers'] += 1
                elif 100000 <= f < 500000:
                    tiers['large_influencers'] += 1
                elif 500000 <= f < 1000000:
                    tiers['mega_influencers'] += 1
                elif f >= 1000000:
                    tiers['celebrities'] += 1
            
            stats['influencer_tiers'] = tiers
            
            # Find high-engagement potential (high follower, low following)
            high_engagement = []
            for p in profiles:
                followers = p.get('followers_count', 0)
                following = p.get('following_count', 0)
                if followers > 5000 and following < 1000:
                    high_engagement.append({
                        'username': p['username'],
                        'followers_count': followers,
                        'following_count': following,
                        'ratio': followers / following if following > 0 else followers
                    })
            
            high_engagement.sort(key=lambda x: x['ratio'], reverse=True)
            stats['high_engagement_potential'] = high_engagement[:20]
        
        # Add timestamp
        stats['analysis_timestamp'] = time.time()
        stats['analysis_date'] = time.strftime('%Y-%m-%d %H:%M:%S')
        
        return stats
    
    def save_analysis(self, stats: Dict[str, Any]):
        """Save analysis results to files.
        
        Args:
            stats: Analysis statistics dict
        """
        # Save JSON
        safe_json_write(self.stats_file, stats)
        print(f"[ANALYSIS] Saved JSON stats to {self.stats_file}")
        
        # Save CSV with all profiles
        self._save_csv(stats)
    
    def _save_csv(self, stats: Dict[str, Any]):
        """Save profile data to CSV."""
        profiles = self.metadata_manager.get_all_profiles().values()
        
        fieldnames = [
            'username', 'full_name', 'followers_count', 'following_count',
            'is_public', 'is_verified', 'biography', 'external_url',
            'media_count', 'last_collected', 'collected_by_account'
        ]
        
        with open(self.csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            for profile in profiles:
                row = {k: profile.get(k, '') for k in fieldnames}
                # Format timestamp
                ts = row.get('last_collected')
                if ts:
                    row['last_collected'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
                writer.writerow(row)
        
        print(f"[ANALYSIS] Saved CSV data to {self.csv_file}")
    
    def print_summary(self, stats: Dict[str, Any]):
        """Print formatted summary of analysis.
        
        Args:
            stats: Analysis statistics dict
        """
        print("\n" + "="*60)
        print("NETWORK PROFILE ANALYSIS")
        print("="*60)
        
        print(f"\nTotal Profiles Tracked: {stats.get('total_profiles', 0)}")
        print(f"Public Profiles: {stats.get('public_profiles', 0)}")
        print(f"Private Profiles: {stats.get('private_profiles', 0)}")
        print(f"Verified Accounts: {stats.get('verified_profiles', 0)}")
        
        print(f"\nAverage Followers: {stats.get('avg_followers', 0):.0f}")
        print(f"Average Following: {stats.get('avg_following', 0):.0f}")
        print(f"Avg F/F Ratio: {stats.get('avg_follower_to_following_ratio', 0):.2f}")
        
        # Influencer tiers
        tiers = stats.get('influencer_tiers', {})
        if any(tiers.values()):
            print("\n--- Influencer Tiers ---")
            for tier, count in tiers.items():
                if count > 0:
                    print(f"  {tier.replace('_', ' ').title()}: {count}")
        
        # Top followers
        print("\n--- Top 10 by Followers ---")
        for i, p in enumerate(stats.get('top_followers', [])[:10], 1):
            verified = " ✓" if p.get('is_verified') else ""
            print(f"  {i}. @{p['username']}{verified}: {p.get('followers_count', 0):,} followers")
        
        # Top following
        print("\n--- Top 10 by Following ---")
        for i, p in enumerate(stats.get('top_following', [])[:10], 1):
            print(f"  {i}. @{p['username']}: {p.get('following_count', 0):,} following")
        
        # High engagement potential
        high_eng = stats.get('high_engagement_potential', [])
        if high_eng:
            print("\n--- High Engagement Potential ---")
            print("  (High followers, low following - good targets)")
            for i, p in enumerate(high_eng[:5], 1):
                ratio = p.get('ratio', 0)
                print(f"  {i}. @{p['username']}: {p['followers_count']:,}/{p['following_count']:,} (ratio: {ratio:.1f})")
        
        print("\n" + "="*60)
        print(f"Analysis saved to:")
        print(f"  JSON: {self.stats_file}")
        print(f"  CSV: {self.csv_file}")
        print("="*60 + "\n")
    
    def get_influential_users(self, min_followers: int = 10000) -> List[Dict[str, Any]]:
        """Get list of influential users meeting criteria.
        
        Args:
            min_followers: Minimum follower count
            
        Returns:
            List of profile dicts
        """
        return [
            p for p in self.metadata_manager.get_all_profiles().values()
            if p.get('followers_count', 0) >= min_followers
        ]
    
    def get_reciprocal_relationships(self) -> List[tuple]:
        """Find mutual follow relationships (requires relationship data).
        
        Returns:
            List of (user1, user2) tuples where both follow each other
        """
        try:
            import os as _os
            from db.manager import DatabaseManager
            from db.repositories.relationship_repository import RelationshipRepository
            db = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
            repo = RelationshipRepository(db)
            relationships = repo.get_relationships()
        except Exception:
            return []
        
        if not relationships:
            return []
        
        # Build follow graph
        follows = {}  # user -> set of users they follow
        
        for rel in relationships:
            source = rel.get('source')
            target = rel.get('target')
            rel_type = rel.get('type')
            
            if source and target:
                if rel_type == 'following':
                    follows.setdefault(source, set()).add(target)
                elif rel_type == 'followers':
                    follows.setdefault(target, set()).add(source)
        
        # Find mutual follows
        mutual = []
        for user1, following in follows.items():
            for user2 in following:
                if user2 in follows and user1 in follows[user2]:
                    # Avoid duplicates (user1,user2) and (user2,user1)
                    if (user2, user1) not in mutual:
                        mutual.append((user1, user2))
        
        return mutual


def main():
    """CLI entry point for profile analysis."""
    analyzer = ProfileAnalyzer()
    
    print("[ANALYSIS] Starting profile network analysis...")
    
    stats = analyzer.analyze_network()
    analyzer.save_analysis(stats)
    analyzer.print_summary(stats)
    
    # Check if we have any data
    if stats.get('total_profiles', 0) == 0:
        print("\n[WARNING] No profile metadata found.")
        print("Run spider operations to collect profile data first.")
        print("Metadata will be automatically saved during spider runs.")


if __name__ == "__main__":
    main()


