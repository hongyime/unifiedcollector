#!/usr/bin/env python3
"""
Target Channels Scraper
=======================
Reads target_channels.txt and scrapes all their videos into the database.
"""

import os
import yt_dlp

from app_paths import TARGET_CHANNELS_FILE
from data_manager_streamlined import db_manager

def scrape_target_channels():
    target_file = str(TARGET_CHANNELS_FILE)
    if not os.path.exists(target_file):
        print(f"❌ No target channels file found at {target_file}")
        print("Please create one or use the Manage Target Channels option in the menu.")
        return

    with open(target_file, 'r', encoding='utf-8') as f:
        channels = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    if not channels:
        print("⚠️  No channel IDs found in target_channels.txt")
        return

    print(f"🎯 Found {len(channels)} target channels to scrape.\n")

    for channel_id in channels:
        # Check if it's a full URL or just an ID
        if channel_id.startswith('http'):
            url = channel_id
        else:
            url = f"https://www.youtube.com/channel/{channel_id}"
            
        print(f"🔄 Scraping channel: {url}")
        
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'remote_components': ['ejs:github'],
            'js_runtimes': {'node': {}},
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    print(f"❌ Failed to extract info for {channel_id}")
                    continue
                    
                entries = info.get('entries', [])
                if not entries:
                    print(f"⚠️  No videos found for {channel_id}")
                    continue
                
                videos_to_add = []
                uploader_name = info.get('uploader') or info.get('title') or info.get('channel') or channel_id
                
                for entry in entries:
                    if entry and entry.get('url'):
                        videos_to_add.append({
                            'url': entry['url'],
                            'title': entry.get('title'),
                            'channel': uploader_name,
                            'channel_id': channel_id,
                            'duration': entry.get('duration')
                        })
                
                if videos_to_add:
                    added_count = db_manager.batch_add_videos(videos_to_add)
                    print(f"✅ Added {added_count} new videos from {uploader_name} (Total scraped: {len(videos_to_add)})")
                
        except Exception as e:
            print(f"❌ Error scraping {channel_id}: {e}")
            
    print("\n🎉 Target channels scraping complete!")

def main():
    """Main entry point."""
    print("🎯 TARGET CHANNELS SCRAPER")
    print("=" * 50)
    print("Scraping channels from target_channels.txt")
    print()
    scrape_target_channels()

if __name__ == '__main__':
    main()
