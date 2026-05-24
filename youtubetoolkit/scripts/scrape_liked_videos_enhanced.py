#!/usr/bin/env python3
"""
Enhanced Liked Videos Scraper - Database Integration
Scrapes liked videos and adds directly to database
"""

import os
import sys
import json
from datetime import datetime
from typing import List, Dict, Optional, Any, Union
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from app_paths import APP_DATA_DIR, CLIENT_SECRET_FILE as APP_CLIENT_SECRET_FILE
from auth_cache import get_primary_credentials_path, load_cached_credentials, save_cached_credentials
from data_manager_streamlined import DatabaseManager

DATA_DIR = str(APP_DATA_DIR)
CLIENT_SECRET = str(APP_CLIENT_SECRET_FILE)
OAUTH_CACHE_FILE = str(get_primary_credentials_path())

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
LIKED_VIDEOS_PLAYLIST_ID = 'LL'

class LikedVideosProcessor:
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
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
                    creds = flow.run_local_server(port=0, open_browser=True)
                    print("🎉 Authentication completed successfully!")
                except Exception as e:
                    print(f"❌ Authentication failed: {e}")
                    return None
            
            # Save the credentials for the next run
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
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

    def get_liked_videos(self, max_videos: int = 1000, days: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get liked videos with metadata"""
        youtube = self.get_authenticated_service()
        if not youtube:
            return []
            
        print(f"👍 Fetching liked videos" + (f" from past {days} days" if days else "") + "...")
        
        cutoff_date = None
        if days:
            from datetime import timedelta, timezone
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            
        videos = []
        nextPageToken = None
        fetched = 0
        
        try:
            while fetched < max_videos:
                # Get liked videos playlist items
                request = youtube.playlistItems().list(
                    part='snippet',
                    playlistId=LIKED_VIDEOS_PLAYLIST_ID,
                    maxResults=min(50, max_videos - fetched),
                    pageToken=nextPageToken
                )
                response = request.execute()
                
                if not response.get('items'):
                    break
                
                items_to_process = []
                stop_fetching = False
                
                # Process each video
                for item in response['items']:
                    if fetched >= max_videos:
                        stop_fetching = True
                        break
                        
                    if cutoff_date:
                        added_at_str = item['snippet']['publishedAt']
                        try:
                            added_at = datetime.fromisoformat(added_at_str.replace('Z', '+00:00'))
                            if added_at < cutoff_date:
                                print(f"\n   ⏱️ Reached videos older than {days} days. Stopping scrape.")
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
                                duration = self.parse_duration(duration_str)
                                
                                videos.append({
                                    'url': video_url,
                                    'video_id': video_id,
                                    'title': video_info['snippet']['title'],
                                    'channel': video_info['snippet']['channelTitle'],
                                    'duration': duration,
                                    'published_at': item['snippet']['publishedAt'],
                                    'metadata': {
                                        'channel_id': video_info['snippet']['channelId'],
                                        'description': video_info['snippet'].get('description', ''),
                                        'view_count': video_info['statistics'].get('viewCount', 0),
                                        'like_count': video_info['statistics'].get('likeCount', 0),
                                        'source': 'liked_videos_processor'
                                    }
                                })
                            else:
                                # Add basic info if details not found
                                videos.append({
                                    'url': video_url,
                                    'video_id': video_id,
                                    'title': item['snippet']['title'],
                                    'channel': item['snippet'].get('videoOwnerChannelTitle', item['snippet'].get('channelTitle', 'Unknown')),
                                    'duration': 0,
                                    'metadata': {'source': 'liked_videos_processor'}
                                })
                    except Exception as e:
                        print(f"⚠️  Error getting batch details: {e}")
                        # Add basic info anyway as fallback
                        for item in items_to_process:
                            video_id = item['snippet']['resourceId']['videoId']
                            videos.append({
                                'url': f"https://www.youtube.com/watch?v={video_id}",
                                'video_id': video_id,
                                'title': item['snippet']['title'],
                                'channel': item['snippet'].get('videoOwnerChannelTitle', item['snippet'].get('channelTitle', 'Unknown')),
                                'duration': 0,
                                'metadata': {'source': 'liked_videos_processor'}
                            })
                
                if stop_fetching:
                    break
                
                nextPageToken = response.get('nextPageToken')
                if not nextPageToken:
                    break
                    
        except Exception as e:
            print(f"❌ Error fetching liked videos: {e}")
            return []
        
        print(f"✅ Found {len(videos)} liked videos")
        return videos

    @staticmethod
    def parse_duration(duration_str: str) -> int:
        """Parse ISO 8601 duration to seconds"""
        try:
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

    def process_liked_videos(self, max_videos: int = 1000, auto_add_to_db: bool = True, days: Optional[int] = None) -> bool:
        """Complete workflow: get liked videos → add to database"""
        print("👍 LIKED VIDEOS PROCESSING WORKFLOW")
        print("=" * 50)
        
        # Get liked videos
        videos = self.get_liked_videos(max_videos, days)
        if not videos:
            print("❌ No liked videos found")
            return False
        
        total_videos = len(videos)
        new_videos = 0
        
        if auto_add_to_db:
            print(f"\n💾 Adding {total_videos} videos to database...")
            
            # Add videos directly to database in batch
            new_videos = self.db.batch_add_videos(videos)
        
        # Summary
        print(f"\n🎉 PROCESSING COMPLETE!")
        print(f"📊 Total liked videos found: {total_videos}")
        if auto_add_to_db:
            print(f"💾 New videos added to database: {new_videos}")
            print(f"🔄 Duplicates skipped: {total_videos - new_videos}")
            print(f"🗃️  Database ready for download processing")
        
        return True

def main():
    """Main execution"""
    import argparse
    parser = argparse.ArgumentParser(description='Enhanced Liked Videos Processor')
    parser.add_argument('--max-videos', type=int, default=1000, help='Max videos to process')
    parser.add_argument('--days', type=int, help='Restrict to past N days')
    parser.add_argument('--no-auto-add', action='store_true', help='Do not automatically add to database')
    args = parser.parse_args()
    
    print("👍 ENHANCED LIKED VIDEOS PROCESSOR")
    print("=" * 50)
    print("This will scrape your liked videos and add them directly to the database")
    print()
    
    # Process liked videos
    processor = LikedVideosProcessor()
    success = processor.process_liked_videos(
        max_videos=args.max_videos,
        auto_add_to_db=not args.no_auto_add,
        days=args.days
    )
    
    if success and not args.no_auto_add:
        print("\n🚀 NEXT STEPS:")
        print("   • Run batch_downloader.py to download queued videos")
        print("   • Videos are now ready in the database")

if __name__ == '__main__':
    main()
