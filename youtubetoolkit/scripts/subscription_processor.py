#!/usr/bin/env python3
"""
Enhanced Subscription Processor - Streamlined Workflow
Handles the complete subscription workflow: scrape → database → process
"""

import os
import sys
import json
from datetime import datetime
from typing import List, Dict, Optional, Any
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from app_paths import (
    APP_DATA_DIR,
    CLIENT_SECRET_FILE as APP_CLIENT_SECRET_FILE,
    SUBSCRIPTIONS_FILE as APP_SUBSCRIPTIONS_FILE,
)
from auth_cache import get_primary_credentials_path, load_cached_credentials, save_cached_credentials
from data_manager_streamlined import DatabaseManager

DATA_DIR = str(APP_DATA_DIR)
CLIENT_SECRET = str(APP_CLIENT_SECRET_FILE)
OAUTH_CACHE_FILE = str(get_primary_credentials_path())
SUBSCRIPTION_CACHE = str(APP_SUBSCRIPTIONS_FILE)

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

class SubscriptionProcessor:
    def __init__(self):
        self.db = DatabaseManager()
        self.youtube = None
        
    def get_authenticated_service(self) -> Any:
        """Get authenticated YouTube service"""
        if self.youtube:
            return self.youtube
            
        creds = None
        
        # Load existing credentials if available
        try:
            creds = load_cached_credentials()
            if creds is not None:
                print(f"📄 Loading existing credentials from {OAUTH_CACHE_FILE}...")
        except Exception as e:
            print(f"⚠️  Error loading credentials: {e}")
            creds = None
        
        # If there are no valid credentials available, let the user log in
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print("🔄 Refreshing expired credentials...")
                try:
                    creds.refresh(Request())
                except Exception as e:
                    print(f"⚠️  Error refreshing credentials: {e}")
                    creds = None
            
            if not creds:
                print("🔐 Starting authentication flow...")
                print("📝 Your browser will open for authentication.")
                print("✅ Please complete the authorization in your browser.")
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
                    creds = flow.run_local_server(port=0, open_browser=True)
                    print("🎉 Authentication completed successfully!")
                except Exception as e:
                    print(f"❌ Authentication failed: {e}")
                    return None
            
            # Save the credentials for the next run
            try:
                saved_path = save_cached_credentials(creds)
                print(f"💾 Credentials saved for future use: {saved_path}")
            except Exception as e:
                print(f"⚠️  Warning: Could not save credentials: {e}")
        
        try:
            self.youtube = build('youtube', 'v3', credentials=creds)
            return self.youtube
        except Exception as e:
            print(f"❌ Error creating YouTube service: {e}")
            return None

    def get_subscriptions(self, max_channels: int = 999, fetch_details: bool = False) -> List[Dict[str, str]]:
        """Get user's subscriptions with caching
        
        Args:
            max_channels: Maximum number of channels to return
            fetch_details: If True, fetch channel details (subscribers, bio)
        """
        print("📋 Fetching your subscriptions...")
        
        # Try to load from cache first (valid for 1 day)
        cache_needs_refresh = False
        if os.path.exists(SUBSCRIPTION_CACHE):
            try:
                with open(SUBSCRIPTION_CACHE, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    cache_time = datetime.fromisoformat(cache_data['timestamp'])
                    
                    # Check if cache has details and if we need them
                    has_details = any(
                        'subscriber_count' in sub 
                        for sub in cache_data.get('subscriptions', [])
                    )
                    
                    if (datetime.now() - cache_time).days < 1:
                        if fetch_details and not has_details:
                            print("🔄 Cache exists but missing details, refreshing...")
                            cache_needs_refresh = True
                        else:
                            print(f"📱 Using cached subscriptions ({len(cache_data['subscriptions'])} channels)")
                            return cache_data['subscriptions'][:max_channels]
            except Exception as e:
                print(f"⚠️  Cache error: {e}")
        
        # Fetch fresh subscriptions
        youtube = self.get_authenticated_service()
        if not youtube:
            return []
            
        subscriptions = []
        nextPageToken = None
        
        try:
            while len(subscriptions) < max_channels:
                response = youtube.subscriptions().list(
                    part="snippet",
                    mine=True,
                    maxResults=50,
                    pageToken=nextPageToken
                ).execute()
                
                for item in response['items']:
                    if len(subscriptions) >= max_channels:
                        break
                    channel_id = item['snippet']['resourceId']['channelId']
                    subscriptions.append({
                        'channel_id': channel_id,
                        'channel_name': item['snippet']['title'],
                        'channel_url': f"https://www.youtube.com/channel/{channel_id}"
                    })
                
                nextPageToken = response.get('nextPageToken')
                if not nextPageToken:
                    break
        
        except Exception as e:
            print(f"❌ Error fetching subscriptions: {e}")
            return []
        
        # Fetch channel details if requested
        if fetch_details and subscriptions:
            print("📊 Fetching channel details...")
            subscriptions = self._fetch_channel_details(youtube, subscriptions)
        
        # Cache the results
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            
            # Load existing cache to preserve last_scrape_time
            existing_last_scrape = None
            if os.path.exists(SUBSCRIPTION_CACHE):
                try:
                    with open(SUBSCRIPTION_CACHE, 'r', encoding='utf-8') as f:
                        existing_cache = json.load(f)
                        existing_last_scrape = existing_cache.get('last_scrape_time')
                except:
                    pass
            
            cache_data = {
                'timestamp': datetime.now().isoformat(),
                'subscriptions': subscriptions,
                'last_scrape_time': existing_last_scrape  # Preserve last scrape time
            }
            with open(SUBSCRIPTION_CACHE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2)
        except Exception as e:
            print(f"⚠️  Could not cache subscriptions: {e}")
        
        print(f"✅ Found {len(subscriptions)} subscriptions")
        return subscriptions
    
    def _fetch_channel_details(self, youtube, subscriptions: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Fetch channel details (subscribers, bio) for a list of subscriptions
        
        Args:
            youtube: Authenticated YouTube service
            subscriptions: List of subscription dicts to enhance
            
        Returns:
            Enhanced subscription list with added subscriber_count and bio fields
        """
        enhanced_subscriptions = []
        
        # Process in batches of 50 (API limit)
        for i in range(0, len(subscriptions), 50):
            batch = subscriptions[i:i+50]
            channel_ids = [s['channel_id'] for s in batch]
            
            try:
                # Fetch channel details
                response = youtube.channels().list(
                    part="snippet,statistics",
                    id=','.join(channel_ids)
                ).execute()
                
                # Create a map of channel_id -> details
                details_map = {}
                for item in response.get('items', []):
                    details_map[item['id']] = {
                        'bio': item['snippet'].get('description', ''),
                        'subscriber_count': item['statistics'].get('subscriberCount', '0')
                    }
                
                # Merge details into subscriptions
                for sub in batch:
                    enhanced_sub = sub.copy()
                    details = details_map.get(sub['channel_id'], {})
                    enhanced_sub['bio'] = details.get('bio', '')
                    enhanced_sub['subscriber_count'] = int(details.get('subscriber_count', 0))
                    enhanced_subscriptions.append(enhanced_sub)
                    
            except Exception as e:
                print(f"⚠️  Error fetching details for batch {i//50}: {e}")
                # Fall back to original subscriptions without details
                enhanced_subscriptions.extend(batch)
            
            # Small delay to avoid rate limiting
            if i + 50 < len(subscriptions):
                import time
                time.sleep(0.1)
        
        return enhanced_subscriptions
    
    def get_last_scrape_time(self) -> Optional[datetime]:
        """Get the timestamp of the last successful scrape"""
        try:
            if os.path.exists(SUBSCRIPTION_CACHE):
                with open(SUBSCRIPTION_CACHE, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    last_scrape = cache_data.get('last_scrape_time')
                    if last_scrape:
                        return datetime.fromisoformat(last_scrape)
        except Exception as e:
            print(f"⚠️  Error reading last scrape time: {e}")
        return None
    
    def update_last_scrape_time(self):
        """Update the last scrape timestamp to now"""
        try:
            if os.path.exists(SUBSCRIPTION_CACHE):
                with open(SUBSCRIPTION_CACHE, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                
                cache_data['last_scrape_time'] = datetime.now().isoformat()
                
                with open(SUBSCRIPTION_CACHE, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, indent=2)
                    
                print(f"✅ Updated last scrape time")
        except Exception as e:
            print(f"⚠️  Could not update last scrape time: {e}")

    def get_channel_videos(self, channel_id: str, channel_name: str, max_videos: int = 0, days: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get recent videos from a channel with metadata
        
        Args:
            channel_id: YouTube channel ID
            channel_name: Channel display name
            max_videos: Maximum videos to fetch (0 = unlimited, get all videos)
            days: Only get videos from last N days (None = all time)
        """
        try:
            cutoff_date = None
            if days:
                from datetime import timedelta, timezone
                cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

            youtube = self.get_authenticated_service()
            if not youtube:
                return []
                
            # Get uploads playlist ID
            try:
                channel_response = youtube.channels().list(
                    part='contentDetails,snippet',
                    id=channel_id
                ).execute()
                
                if not channel_response['items']:
                    print(f"⚠️  Channel not found: {channel_name}")
                    return []
                    
                uploads_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
            except Exception as e:
                print(f"⚠️  Error getting channel info for {channel_name}: {e}")
                return []
            
            # Get videos from uploads playlist
            videos = []
            nextPageToken = None
            fetched = 0
            
            while max_videos == 0 or fetched < max_videos:
                try:
                    request = youtube.playlistItems().list(
                        part='snippet',
                        playlistId=uploads_playlist_id,
                        maxResults=50 if max_videos == 0 else min(50, max_videos - fetched),
                        pageToken=nextPageToken
                    )
                    response = request.execute()
                    
                    # Show progress for unlimited scraping
                    if max_videos == 0 and fetched > 0:
                        print(f"      Fetched {fetched} videos so far...", end='\r')
                except Exception as e:
                    # Handle 404 or other playlist errors
                    if '404' in str(e) or 'playlistNotFound' in str(e):
                        print(f"⚠️  Uploads playlist is private or unavailable for {channel_name}")
                    else:
                        print(f"⚠️  Error fetching playlist for {channel_name}: {e}")
                    break
                
                items_to_process = []
                stop_fetching = False
                
                for item in response['items']:
                    if max_videos > 0 and fetched >= max_videos:
                        stop_fetching = True
                        break
                        
                    if cutoff_date:
                        added_at_str = item['snippet']['publishedAt']
                        try:
                            added_at = datetime.fromisoformat(added_at_str.replace('Z', '+00:00'))
                            if added_at < cutoff_date:
                                # We've reached videos older than our cutoff, stop fetching for this channel
                                stop_fetching = True
                                break
                        except Exception as e:
                            pass
                            
                    items_to_process.append(item)
                    fetched += 1
                
                if items_to_process:
                    video_ids = [item['snippet']['resourceId']['videoId'] for item in items_to_process]
                    
                    try:
                        # Get additional video details in a single batch request
                        video_details = youtube.videos().list(
                            part='snippet,contentDetails,statistics',
                            id=','.join(video_ids)
                        ).execute()
                        
                        details_map = {v['id']: v for v in video_details.get('items', [])}
                        
                        for item in items_to_process:
                            video_id = item['snippet']['resourceId']['videoId']
                            video_url = f"https://www.youtube.com/watch?v={video_id}"
                            
                            video_info = details_map.get(video_id)
                            if video_info:
                                duration_str = video_info['contentDetails']['duration']
                                # Convert ISO 8601 duration to seconds (simplified)
                                duration = self.parse_duration(duration_str)
                                
                                videos.append({
                                    'url': video_url,
                                    'video_id': video_id,
                                    'title': video_info['snippet']['title'],
                                    'channel': channel_name,
                                    'duration': duration,
                                    'published_at': item['snippet']['publishedAt'],
                                    'metadata': {
                                        'channel_id': channel_id,
                                        'description': video_info['snippet'].get('description', ''),
                                        'view_count': video_info['statistics'].get('viewCount', 0),
                                        'like_count': video_info['statistics'].get('likeCount', 0),
                                        'source': 'subscription_processor'
                                    }
                                })
                    except Exception as e:
                        print(f"⚠️  Error getting batch details for {channel_name}: {e}")
                
                # Check if we should continue pagination
                if stop_fetching:
                    break
                    
                nextPageToken = response.get('nextPageToken')
                if not nextPageToken:
                    break  # No more pages
                
                if stop_fetching:
                    break
                    
                nextPageToken = response.get('nextPageToken')
                if not nextPageToken:
                    break
                    
            return videos
            
        except Exception as e:
            print(f"⚠️  Error getting videos from {channel_name}: {e}")
            return []

    @staticmethod
    def parse_duration(duration_str: str) -> int:
        """Parse ISO 8601 duration to seconds"""
        try:
            # Simple parser for PT#M#S format
            import re
            match = re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
            if match:
                hours = int(match.group(1) or 0)
                minutes = int(match.group(2) or 0) 
                seconds = int(match.group(3) or 0)
                return hours * 3600 + minutes * 60 + seconds
        except Exception:
            pass
        return 0

    def process_subscriptions(self, max_videos_per_channel: int = 0, max_channels: int = 999, auto_add_to_db: bool = True, days: Optional[int] = None, since_last_scrape: bool = False) -> bool:
        """Complete workflow: get subscriptions → extract videos → add to database
        
        Args:
            max_videos_per_channel: Max videos per channel (0 = unlimited, get all)
            max_channels: Max channels to process
            auto_add_to_db: Automatically add videos to database
            days: Only get videos from last N days (None = all time)
            since_last_scrape: Only get videos since last scrape (overrides days)
        """
        print("🚀 SUBSCRIPTION PROCESSING WORKFLOW")
        print("=" * 50)
        
        # Handle since_last_scrape mode
        if since_last_scrape:
            last_scrape = self.get_last_scrape_time()
            if last_scrape:
                days_since = (datetime.now() - last_scrape).days
                print(f"📅 Last scrape was {days_since} days ago ({last_scrape.strftime('%Y-%m-%d %H:%M')})")
                print(f"🔍 Will fetch videos from the last {days_since + 1} days")
                days = days_since + 1  # Add 1 to include today
            else:
                print("📅 No previous scrape found - will fetch all videos")
                days = None
        
        # Get subscriptions
        subscriptions = self.get_subscriptions(max_channels)
        if not subscriptions:
            print("❌ No subscriptions found")
            return False
        
        total_videos = 0
        new_videos = 0
        
        # Process each channel
        for i, channel in enumerate(subscriptions, 1):
            print(f"\n📺 ({i}/{len(subscriptions)}) Processing: {channel['channel_name']}")
            
            videos = self.get_channel_videos(
                channel['channel_id'], 
                channel['channel_name'], 
                max_videos_per_channel,
                days
            )
            
            if videos:
                print(f"   ✅ Found {len(videos)} videos")
                total_videos += len(videos)
                
                if auto_add_to_db:
                    # Add videos directly to database in batch
                    added_count = self.db.batch_add_videos(videos)
                    new_videos += added_count
                    print(f"   💾 Added {added_count} new videos to database")
            else:
                print(f"   ⚠️  No videos found")
        
        # Summary
        print(f"\n🎉 PROCESSING COMPLETE!")
        print(f"📊 Total videos found: {total_videos}")
        if auto_add_to_db:
            print(f"💾 New videos added to database: {new_videos}")
            print(f"🗃️  Database ready for download processing")
            
            # Update last scrape time on successful completion
            if since_last_scrape or days:
                self.update_last_scrape_time()
        
        return True

def main():
    """Main execution"""
    import argparse
    parser = argparse.ArgumentParser(description='Enhanced Subscription Processor')
    parser.add_argument('--max-videos', type=int, default=0, help='Max videos per channel (0 = unlimited, get all videos)')
    parser.add_argument('--max-channels', type=int, default=999, help='Max channels to process')
    parser.add_argument('--days', type=int, help='Restrict to past N days')
    parser.add_argument('--since-last-scrape', action='store_true', help='Only get videos since last scrape (smart mode)')
    parser.add_argument('--no-auto-add', action='store_true', help='Do not automatically add to database')
    args = parser.parse_args()
    
    print("🎬 ENHANCED SUBSCRIPTION PROCESSOR")
    print("=" * 50)
    print("This will scrape your subscriptions and add videos directly to the database")
    print()
    
    # Process subscriptions
    processor = SubscriptionProcessor()
    success = processor.process_subscriptions(
        max_videos_per_channel=args.max_videos,
        max_channels=args.max_channels,
        auto_add_to_db=not args.no_auto_add,
        days=args.days,
        since_last_scrape=args.since_last_scrape
    )
    
    if success and not args.no_auto_add:
        print("\n🚀 NEXT STEPS:")
        print("   • Run batch_downloader.py to process queued videos")
        print("   • Videos are now ready in the database")
        print("   • No manual file management needed!")

if __name__ == '__main__':
    main()
