#!/usr/bin/env python3
"""
Custom Playlist/Channel Scraper
Scrapes any YouTube Playlist or Channel URL and adds videos to the database
"""

import sys
import argparse
import yt_dlp
from data_manager_streamlined import db_manager

def scrape_custom_url(url, auto_add=True, days=None):
    print(f"🔍 Scraping: {url}")
    print("=" * 50)

    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'remote_components': ['ejs:github'],
        'js_runtimes': {'node': {}},
    }
    
    if days:
        ydl_opts['dateafter'] = f"now-{days}days"
    
    videos = []
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            
            if 'entries' in info:
                # It's a playlist or channel
                entries = list(info['entries'])
                for entry in entries:
                    if not entry: continue
                    video_url = entry.get('url')
                    if not video_url:
                        video_id = entry.get('id')
                        if video_id:
                            video_url = f"https://www.youtube.com/watch?v={video_id}"
                        else:
                            continue
                            
                    videos.append({
                        'url': video_url,
                        'title': entry.get('title', 'Unknown'),
                        'channel': entry.get('uploader', info.get('uploader', 'Unknown')),
                        'channel_id': entry.get('channel_id', info.get('channel_id')),
                        'duration': entry.get('duration', 0),
                        'metadata': {
                            'source': 'custom_scraper',
                            'playlist_title': info.get('title', '')
                        }
                    })
            else:
                # Single video
                videos.append({
                    'url': info.get('webpage_url', url),
                    'title': info.get('title', 'Unknown'),
                    'channel': info.get('uploader', 'Unknown'),
                    'channel_id': info.get('channel_id'),
                    'duration': info.get('duration', 0),
                    'metadata': {
                        'source': 'custom_scraper'
                    }
                })
                
        except Exception as e:
            print(f"❌ Error scraping URL: {e}")
            return False
            
    if not videos:
        print("❌ No videos found at the provided URL")
        return False
        
    print(f"✅ Found {len(videos)} videos")
    
    if auto_add:
        print(f"\n💾 Adding {len(videos)} videos to database...")
        added_count = db_manager.batch_add_videos(videos)
        print(f"   💾 Added {added_count} new videos to database")
        print(f"   🔄 Duplicates skipped: {len(videos) - added_count}")
        
    return True

def main():
    print("🎯 CUSTOM PLAYLIST/CHANNEL SCRAPER")
    print("=" * 50)
    print("Scrape any YouTube Playlist or Channel URL directly into your database")
    print()
    
    parser = argparse.ArgumentParser(description='Scrape custom YouTube URLs')
    parser.add_argument('url', nargs='?', help='YouTube Playlist or Channel URL')
    parser.add_argument('--no-auto-add', action='store_true', help='Do not automatically add to database')
    parser.add_argument('--days', type=int, help='Restrict to past N days')
    
    args = parser.parse_args()
    
    url = args.url
    if not url:
        url = input("📝 Enter YouTube Playlist or Channel URL: ").strip()
        
    if not url:
        print("❌ No URL provided")
        return
        
    scrape_custom_url(url, not args.no_auto_add, args.days)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Cancelled by user")
