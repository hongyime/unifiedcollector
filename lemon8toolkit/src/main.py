"""
Unified Lemon8 Toolkit - Main CLI Interface
"""
import argparse
import glob as _glob
import os
import signal
import sys
from datetime import datetime
from typing import Dict, Any, List, Optional

from config import ensure_data_directory
from scraper import Lemon8Scraper
from downloader import MediaDownloader
from tracking import UnifiedTracker
from progress import ProgressManager
from path_manager import prompt_for_download_path
from graph_builder import GraphBuilder

class Lemon8Toolkit:
    def __init__(self, auto_save: bool = True):
        ensure_data_directory()
        self.scraper = Lemon8Scraper()
        # Share the scraper's session with the downloader for better cookie/auth handling
        self.downloader = MediaDownloader(session=self.scraper.session, auto_save=auto_save)
        self.tracker = UnifiedTracker(auto_save=auto_save)
        self.progress = ProgressManager(auto_save=auto_save)
    
    def save(self):
        """Flush all buffered data to disk"""
        self.downloader.save()
        self.tracker.save()
        self.progress.save()

    def _ensure_download_path(self) -> str:
        """Resolve and cache the download directory right now.

        Calling this at the start of any feature that downloads means the user
        is prompted once, up-front, and can then walk away while the toolkit
        runs autonomously.  Subsequent calls are instant no-ops (cached).
        """
        return self.downloader._get_downloads_dir()
    
    def scrape_user(
        self,
        username: Optional[str] = None,
        user_id: Optional[str] = None,
        download_media: bool = True,
        force_rescrape: bool = False,
        include_profile_photos: Optional[bool] = None,
    ):
        """Scrape a user profile and optionally download media"""
        # Resolve username from user_id if provided
        if user_id and not username:
            username = self.tracker.account_tracker.resolve_username_from_id(user_id)
            if not username:
                print(f"❌ No username found for user_id: {user_id}")
                return
            print(f"🔍 Resolved user_id {user_id} to username: @{username}")
        
        if not username:
            # Show discovered users list
            discovered = self.tracker.account_tracker.get_discovered_users()
            if not discovered:
                print("ℹ️ No discovered users found in database yet. Try scraping the feed first!")
                return
            
            print("\n📋 Discovered Users (Not yet scraped):")
            for i, user in enumerate(discovered[:20], 1):
                print(f"{i}. @{user}")
            
            if len(discovered) > 20:
                print(f"... and {len(discovered) - 20} more")
            
            try:
                choice = input("\nEnter number to scrape (or 'q' to quit): ").strip().lower()
                if choice == 'q':
                    return
                idx = int(choice) - 1
                if 0 <= idx < min(len(discovered), 20):
                    username = discovered[idx]
                else:
                    print("❌ Invalid selection")
                    return
            except (ValueError, IndexError):
                print("❌ Invalid input")
                return

        username = username.lstrip('@')

        # Resolve download path upfront so the user can walk away
        if download_media:
            self._ensure_download_path()

        # Check if user already visited
        if not force_rescrape and self.tracker.account_tracker.is_user_visited(username):
            print(f"⏭️ User @{username} already visited. Use --force to rescrape.")
            user_info = self.tracker.account_tracker.get_user_info(username)
            if user_info:
                print(f"📊 Last visited: {user_info.get('last_visited', 'Unknown')}")
                if user_info.get('user_id'):
                    print(f"🆔 User ID: {user_info['user_id']}")
            return
        
        # Start progress session
        session_id = self.progress.start_session('user', username)
        
        try:
            # Scrape user profile
            print(f"\n🔍 Scraping user profile: @{username}")
            scrape_result = self.scraper.scrape_user_profile(
                username,
                include_profile_images=include_profile_photos,
            )
            
            if 'error' in scrape_result:
                print(f"❌ Scraping failed: {scrape_result['error']}")
                self.progress.end_session(session_id, 'failed')
                return
            
            media_items = scrape_result.get('media_items') or scrape_result.get('media_urls', [])
            media_urls = [
                item['url'] if isinstance(item, dict) else item
                for item in media_items
            ]
            
            # Update progress with scraped media
            self.progress.update_session_scraped_media(session_id, media_urls)
            
            # Mark user as visited
            hashtag_count = len(scrape_result.get('hashtags', []))
            topic_id_count = len(scrape_result.get('tag_ids', []))
            user_metadata = {
                'total_media_found': len(media_urls),
                'related_users_found': len(scrape_result.get('related_users', [])),
                'tags_found': hashtag_count + topic_id_count,
                'hashtags_found': hashtag_count,
                'topic_ids_found': topic_id_count,
                'related_users': list(scrape_result.get('related_users', [])),
            }
            # Add user_id if available
            if scrape_result.get('user_id'):
                user_metadata['user_id'] = scrape_result['user_id']
            
            self.tracker.account_tracker.mark_user_visited(username, user_metadata)
            
            # Create historical snapshot if user_info is available
            user_info = scrape_result.get('user_info', {})
            if user_info:
                self.tracker.account_tracker.create_snapshot(
                    username,
                    user_id=scrape_result.get('user_id'),
                    followers_count=user_info.get('follower_count', 0),
                    following_count=user_info.get('following_count', 0),
                    post_count=user_info.get('post_count', 0)
                )
            
            print(f"✅ User profile scraped successfully!")
            print(f"📊 Found: {len(media_urls)} media files")
            print(f"👥 Discovered: {len(scrape_result.get('related_users', []))} related users")
            print(f"🏷️ Found: {hashtag_count} hashtags, {topic_id_count} topic IDs")
            
            # Download media if requested
            if download_media and media_urls:
                print(f"\n⬇️ Starting media download...")
                # Use profile URL as referer
                from config import get_user_url
                profile_url = get_user_url(username)
                download_results = self.downloader.download_multiple_media(
                    media_items, 'user', username, referer=profile_url
                )
                
                # Update progress with download results
                for url, file_path in download_results.items():
                    if file_path:
                        self.progress.update_session_downloaded_media(session_id, url, file_path)
                    else:
                        self.progress.update_session_failed_download(session_id, url, "Download failed or skipped")
            
            # Display discovered content
            if scrape_result.get('related_users'):
                print(f"\n👥 Related users found: {', '.join(['@' + u for u in scrape_result['related_users'][:10]])}")
                if len(scrape_result['related_users']) > 10:
                    print(f"    ... and {len(scrape_result['related_users']) - 10} more")
            
            if scrape_result.get('hashtags'):
                print(f"\n🏷️ Hashtags found: {', '.join(['#' + tag for tag in scrape_result['hashtags'][:8]])}")
                if len(scrape_result['hashtags']) > 8:
                    print(f"    ... and {len(scrape_result['hashtags']) - 8} more")

            if scrape_result.get('tag_ids'):
                print(f"\n🔢 Topic IDs found: {', '.join(scrape_result['tag_ids'][:5])}")
                if len(scrape_result['tag_ids']) > 5:
                    print(f"    ... and {len(scrape_result['tag_ids']) - 5} more")
            
            self.progress.end_session(session_id, 'completed')
            
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            self.progress.end_session(session_id, 'failed')
    
    def scrape_feed(
        self,
        pages: int = 10,
        download_media: bool = True,
        include_profile_photos: Optional[bool] = None,
    ):
        """Scrape the For You feed and optionally download media"""

        # Resolve download path upfront so the user can walk away
        if download_media:
            self._ensure_download_path()

        # Start progress session
        session_id = self.progress.start_session('feed', 'foryou', {'pages': pages})
        
        try:
            # Scrape feed
            print(f"\n🔍 Scraping For You feed ({pages} pages)")
            scrape_result = self.scraper.scrape_for_you_feed(
                pages,
                include_profile_images=include_profile_photos,
            )
            
            if 'error' in scrape_result:
                print(f"❌ Scraping failed: {scrape_result['error']}")
                self.progress.end_session(session_id, 'failed')
                return
            
            media_items = scrape_result.get('media_items') or scrape_result.get('media_urls', [])
            media_urls = [
                item['url'] if isinstance(item, dict) else item
                for item in media_items
            ]
            
            # Update progress with scraped media
            self.progress.update_session_scraped_media(session_id, media_urls)
            
            # Store discovered users and tags in database
            discovered_users = scrape_result.get('discovered_users', [])
            discovered_tags = scrape_result.get('discovered_tags', [])
            
            # Add discovered users to tracking database
            users_added = 0
            for username in discovered_users:
                if not self.tracker.account_tracker.is_user_tracked(username):
                    user_metadata: Dict[str, Any] = {
                        'discovered_from': 'feed_scraping',
                        'discovery_session': session_id,
                        'total_media_found': 0,
                        'discovered_at': pages
                    }
                    self.tracker.account_tracker.mark_user_visited(username, user_metadata)
                    users_added += 1
            
            # Add discovered tags to tracking database
            tags_added = 0
            for tag_id in discovered_tags:
                if not self.tracker.tag_tracker.is_tag_tracked(tag_id):
                    tag_metadata: Dict[str, Any] = {
                        'discovered_from': 'feed_scraping',
                        'discovery_session': session_id,
                        'total_media_found': 0,
                        'discovered_at': pages
                    }
                    self.tracker.tag_tracker.mark_tag_processed(tag_id, tag_metadata)
                    tags_added += 1
            
            print(f"✅ Feed scraped successfully!")
            print(f"📊 Found: {len(media_urls)} media files")
            print(f"👥 Discovered: {len(discovered_users)} users ({users_added} new)")
            print(f"🏷️ Found: {len(discovered_tags)} tags ({tags_added} new)")
            
            if users_added > 0:
                print(f"💾 Added {users_added} new users to database")
            if tags_added > 0:
                print(f"💾 Added {tags_added} new tags to database")
            
            # Download media if requested
            if download_media and media_urls:
                print(f"\n⬇️ Starting media download...")
                # Use feed URL as referer for better download success
                from config import FEED_URL
                download_results = self.downloader.download_multiple_media(
                    media_items, 'feed', 'foryou', referer=FEED_URL
                )
                
                # Update progress with download results
                for url, file_path in download_results.items():
                    if file_path:
                        self.progress.update_session_downloaded_media(session_id, url, file_path)
                    else:
                        self.progress.update_session_failed_download(session_id, url, "Download failed or skipped")
            
            self.progress.end_session(session_id, 'completed')
            
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            self.progress.end_session(session_id, 'failed')
    
    def scrape_tag(
        self,
        tag_id: str,
        download_media: bool = True,
        force_rescrape: bool = False,
        pages: int = 10,
    ):
        """Scrape a tag/topic and optionally download media"""
        
        # Check if tag already processed
        if not force_rescrape and self.tracker.tag_tracker.is_tag_processed(tag_id):
            print(f"⏭️ Tag {tag_id} already processed. Use --force to rescrape.")
            tag_info = self.tracker.tag_tracker.get_tag_info(tag_id)
            if tag_info:
                print(f"📊 Last processed: {tag_info.get('last_processed', 'Unknown')}")
            return

        # Resolve download path upfront so the user can walk away
        if download_media:
            self._ensure_download_path()

        # Start progress session
        session_id = self.progress.start_session('tag', tag_id, {'pages': pages})
        
        try:
            # Scrape tag
            print(f"\n🔍 Scraping tag/topic: {tag_id} ({pages} pages)")
            scrape_result = self.scraper.scrape_tag_topic(tag_id, pages=pages)
            
            if 'error' in scrape_result:
                print(f"❌ Scraping failed: {scrape_result['error']}")
                self.progress.end_session(session_id, 'failed')
                return
            
            media_items = scrape_result.get('media_items') or scrape_result.get('media_urls', [])
            media_urls = [
                item['url'] if isinstance(item, dict) else item
                for item in media_items
            ]
            
            # Update progress with scraped media
            self.progress.update_session_scraped_media(session_id, media_urls)
            
            # Mark tag as processed
            tag_metadata = {
                'total_media_found': len(media_urls),
                'related_users_found': len(scrape_result.get('related_users', [])),
                'related_tags_found': len(scrape_result.get('related_tags', [])),
                'related_users': list(scrape_result.get('related_users', [])),
            }
            self.tracker.tag_tracker.mark_tag_processed(tag_id, tag_metadata)
            
            print(f"✅ Tag scraped successfully!")
            print(f"📊 Found: {len(media_urls)} media files")
            print(f"👥 Discovered: {len(scrape_result.get('related_users', []))} users")
            print(f"🏷️ Found: {len(scrape_result.get('related_tags', []))} related tags")
            if scrape_result.get('fallback_used'):
                fallback_url = scrape_result.get('fallback_url')
                post_pages = scrape_result.get('fallback_post_pages_scraped', 0)
                print(f"🔎 Keyword fallback used: {fallback_url}")
                if post_pages:
                    print(f"🧩 Parsed {post_pages} related post page(s) for media recovery")
            elif scrape_result.get('topic_no_content_shell') and not media_urls:
                print("ℹ️ This topic currently shows 'No content' on Lemon8 web for your region.")
            
            # Download media if requested
            if download_media and media_urls:
                print(f"\n⬇️ Starting media download...")
                # Use tag URL as referer
                from config import get_tag_url
                tag_url = get_tag_url(tag_id)
                download_results = self.downloader.download_multiple_media(
                    media_items, 'tag', tag_id, referer=tag_url
                )
                
                # Update progress with download results
                for url, file_path in download_results.items():
                    if file_path:
                        self.progress.update_session_downloaded_media(session_id, url, file_path)
                    else:
                        self.progress.update_session_failed_download(session_id, url, "Download failed or skipped")
            
            self.progress.end_session(session_id, 'completed')
            
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            self.progress.end_session(session_id, 'failed')
    
    def seed_from_feed(self, pages: int = 10, download_media: bool = False):
        """Seed spider queue from For You feed"""
        print(f"\n🌱 Seeding from For You feed ({pages} pages)")
        
        # Scrape feed (without downloading media initially)
        session_id = self.progress.start_session('seed', 'foryou', {'pages': pages})
        
        try:
            scrape_result = self.scraper.scrape_for_you_feed(pages, include_profile_images=False)
            
            if 'error' in scrape_result:
                print(f"❌ Seeding failed: {scrape_result['error']}")
                self.progress.end_session(session_id, 'failed')
                return
            
            discovered_users = scrape_result.get('discovered_users', [])
            
            # Add discovered users to spider queue
            users_queued = 0
            for username in discovered_users:
                if not self.tracker.account_tracker.is_user_tracked(username):
                    user_metadata: Dict[str, Any] = {
                        'discovered_from': 'seed_feed',
                        'discovery_session': session_id,
                        'total_media_found': 0
                    }
                    self.tracker.account_tracker.mark_user_visited(username, user_metadata)
                    users_queued += 1
            
            print(f"✅ Seeding complete!")
            print(f"👥 Discovered: {len(discovered_users)} users ({users_queued} new)")
            print(f"🕷️ Spider queue now has {len(self.tracker.account_tracker.get_pending_spider_users())} pending users")
            
            self.progress.end_session(session_id, 'completed')
            
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            self.progress.end_session(session_id, 'failed')
    
    def spider_batch(self, batch_size: int = 10, download_media: bool = True):
        """Spider a batch of pending users"""
        # Reset any stuck spiders from previous crashes
        self.tracker.account_tracker.reset_stuck_spiders()
        
        # Get pending users
        pending_users = self.tracker.account_tracker.get_pending_spider_users(batch_size)
        
        if not pending_users:
            print("ℹ️ No pending users in spider queue. Try seeding from feed first!")
            return

        # Resolve download path upfront so the user can walk away for the full batch
        if download_media:
            self._ensure_download_path()

        print(f"\n🕷️ Spidering {len(pending_users)} users")
        
        for i, username in enumerate(pending_users, 1):
            print(f"\n▶️ [{i}/{len(pending_users)}] Spidering @{username}")
            
            # Mark as in progress
            self.tracker.account_tracker.mark_spider_in_progress(username)
            
            try:
                # Scrape user
                self.scrape_user(username, download_media=download_media, force_rescrape=False)
                
                # Mark as completed
                self.tracker.account_tracker.mark_spider_completed(username)
                
            except Exception as e:
                print(f"❌ Error spidering @{username}: {e}")
                # Leave as in_progress - will be reset on next run
        
        print(f"\n✅ Spider batch complete!")
        remaining = len(self.tracker.account_tracker.get_pending_spider_users())
        print(f"🕷️ {remaining} users remaining in spider queue")
    
    def show_stats(self):
        """Show toolkit statistics"""
        print("\n📊 Lemon8 Toolkit Statistics")
        print("=" * 50)
        
        # Progress stats
        progress_stats = self.progress.get_stats()
        print(f"🔄 Sessions: {progress_stats['total_sessions']} total, {progress_stats['completed_sessions']} completed, {progress_stats['in_progress_sessions']} in progress")
        print(f"🎬 Media: {progress_stats['total_media_scraped']} scraped, {progress_stats['total_media_downloaded']} downloaded")
        print(f"📈 Success Rate: {progress_stats['overall_success_rate']:.1f}%")
        
        # Tracking stats
        tracking_stats = self.tracker.get_combined_stats()
        print(f"👥 Users Visited: {tracking_stats['accounts']['total_visited_users']}")
        print(f"🏷️ Tags Processed: {tracking_stats['tags']['total_processed_tags']}")
        
        # Download stats
        download_stats = self.downloader.get_stats()
        print(f"💾 Media Tracked: {download_stats['total_downloaded']}")
        
        # Current session
        current_session = self.progress.get_current_session()
        if current_session:
            print(f"\n⏳ Current Session: {current_session['session_id']}")
            print(f"    Type: {current_session['session_type']}")
            print(f"    Target: {current_session['identifier']}")
            print(f"    Status: {current_session['status']}")

    def clear_all(self):
        """Clear all tracking, progress, and download history"""
        print("\n🧹 Clearing all toolkit data...")
        self.downloader.clear_download_history()
        self.tracker.clear_all_tracking()
        self.progress.clear_progress_history()
        print("✅ All data cleared successfully!")
    
    def build_graph(self, limit: Optional[int] = None):
        """Build network graph from tracked users"""
        print("\n🕸️ Building network graph...")
        graph_builder = GraphBuilder()
        
        try:
            edges_created = graph_builder.build_graph_from_users(limit)
            
            print(f"✅ Graph built successfully!")
            print(f"📊 Edges created:")
            for edge_type, count in edges_created.items():
                print(f"   {edge_type}: {count}")
            
            # Show stats
            stats = graph_builder.get_graph_stats()
            print(f"\n📈 Graph Statistics:")
            print(f"   Total edges: {stats['total_edges']}")
            print(f"   Unique nodes: {stats['unique_nodes']}")
            
        finally:
            graph_builder.close()
    
    def show_graph_stats(self):
        """Show network graph statistics"""
        print("\n🕸️ Network Graph Statistics")
        print("=" * 50)
        
        graph_builder = GraphBuilder()
        try:
            stats = graph_builder.get_graph_stats()
            
            print(f"📊 Total Edges: {stats['total_edges']}")
            print(f"👥 Unique Nodes: {stats['unique_nodes']}")
            
            print(f"\n📈 Edges by Type:")
            for edge_type, count in stats['edges_by_type'].items():
                print(f"   {edge_type}: {count}")
            
            if stats['top_sources']:
                print(f"\n🔝 Top Sources (by outgoing connections):")
                for i, user in enumerate(stats['top_sources'][:5], 1):
                    print(f"   {i}. @{user['source_username']}: {user['connections']} connections")
            
            if stats['top_targets']:
                print(f"\n🎯 Top Targets (by incoming connections):")
                for i, user in enumerate(stats['top_targets'][:5], 1):
                    print(f"   {i}. @{user['target_username']}: {user['connections']} connections")
        
        finally:
            graph_builder.close()
    
    def download_pending_media(self, limit: int = 100):
        """Download pending media from progress database"""
        # Resolve download path upfront so the user can walk away
        self._ensure_download_path()

        print(f"\n⬇️ Downloading pending media (limit: {limit})")
        print("=" * 50)

        # Get all sessions with scraped but not downloaded media
        all_sessions = self.progress.get_all_sessions_summary()
        
        pending_media = []
        for session in all_sessions:
            session_data = self.progress._get_session(session['session_id'])
            if not session_data:
                continue
            
            # Get scraped media URLs
            scraped_urls = set(session_data.get('scraped_media', []))
            
            # Get already downloaded media URLs
            downloaded_urls = set(item['url'] for item in session_data.get('downloaded_media', []))
            
            # Find pending media (scraped but not downloaded)
            pending_urls = scraped_urls - downloaded_urls
            
            for url in pending_urls:
                pending_media.append({
                    'url': url,
                    'session_id': session['session_id'],
                    'session_type': session['session_type'],
                    'identifier': session['identifier']
                })
        
        if not pending_media:
            print("✅ No pending media found. All scraped media has been downloaded!")
            return
        
        # Limit the number of media to download
        pending_media = pending_media[:limit]
        
        print(f"📊 Found {len(pending_media)} pending media items")
        print(f"⬇️ Starting download...\n")
        
        # Download pending media
        downloaded_count = 0
        failed_count = 0
        skipped_count = 0
        
        for i, media_info in enumerate(pending_media, 1):
            url = media_info['url']
            session_type = media_info['session_type']
            identifier = media_info['identifier']
            session_id = media_info['session_id']
            
            print(f"[{i}/{len(pending_media)}] Downloading from {session_type}/{identifier}...")
            
            try:
                # Check if already downloaded (deduplication)
                if self.downloader.is_already_downloaded(url):
                    print(f"⏭️ Already downloaded, skipping")
                    skipped_count += 1
                    continue
                
                # Download the media
                file_path = self.downloader.download_media(
                    url=url,
                    scrape_type=session_type,
                    identifier=identifier,
                    referer=f"https://www.lemon8-app.com/"
                )
                
                if file_path:
                    # Update progress database
                    self.progress.update_session_downloaded_media(session_id, url, file_path)
                    downloaded_count += 1
                    print(f"✅ Downloaded successfully")
                else:
                    # Update progress database with failure
                    self.progress.update_session_failed_download(session_id, url, "Download failed")
                    failed_count += 1
                    print(f"❌ Download failed")
            
            except Exception as e:
                print(f"❌ Error downloading: {e}")
                self.progress.update_session_failed_download(session_id, url, str(e))
                failed_count += 1
        
        # Print summary
        print(f"\n{'=' * 50}")
        print(f"📊 Download Summary:")
        print(f"   ✅ Downloaded: {downloaded_count}")
        print(f"   ⏭️ Skipped (already downloaded): {skipped_count}")
        print(f"   ❌ Failed: {failed_count}")
        print(f"   📈 Total processed: {len(pending_media)}")
        
        # Save progress
        self.save()
    
    def reconcile_missing_files(self, session_id: Optional[str] = None):
        """Reconcile missing files and re-download them"""
        print(f"\n🔄 Reconciling missing files")
        print("=" * 50)
        
        # Get sessions to reconcile
        if session_id:
            session = self.progress._get_session(session_id)
            if not session:
                print(f"❌ Session not found: {session_id}")
                return
            sessions_to_check = [session]
            print(f"📊 Checking session: {session_id}")
        else:
            all_sessions = self.progress.get_all_sessions_summary()
            sessions_to_check = [
                self.progress._get_session(s['session_id']) 
                for s in all_sessions
            ]
            sessions_to_check = [s for s in sessions_to_check if s is not None]
            print(f"📊 Checking all {len(sessions_to_check)} sessions")
        
        # Find missing files
        missing_files = []
        
        for session in sessions_to_check:
            downloaded_media = session.get('downloaded_media', [])
            
            for media_info in downloaded_media:
                file_path = media_info.get('file_path')
                url = media_info.get('url')
                
                if not file_path or not url:
                    continue
                
                # Check if file exists on disk
                if not os.path.exists(file_path):
                    missing_files.append({
                        'url': url,
                        'file_path': file_path,
                        'session_id': session['session_id'],
                        'session_type': session['session_type'],
                        'identifier': session['identifier']
                    })
        
        if not missing_files:
            print("✅ No missing files found. All downloaded media exists on disk!")
            return
        
        print(f"\n⚠️ Found {len(missing_files)} missing files")
        print("\nMissing files:")
        for i, file_info in enumerate(missing_files[:10], 1):
            print(f"  {i}. {os.path.basename(file_info['file_path'])} (from {file_info['session_type']}/{file_info['identifier']})")
        
        if len(missing_files) > 10:
            print(f"  ... and {len(missing_files) - 10} more")
        
        # Ask user if they want to re-download
        try:
            choice = input(f"\n🔄 Re-download {len(missing_files)} missing files? (y/n): ").strip().lower()
            if choice != 'y':
                print("❌ Reconciliation cancelled")
                return
        except (EOFError, KeyboardInterrupt):
            print("\n❌ Reconciliation cancelled")
            return

        # Resolve download path upfront so the user can walk away
        self._ensure_download_path()

        # Re-download missing files
        print(f"\n⬇️ Re-downloading {len(missing_files)} missing files...\n")
        
        downloaded_count = 0
        failed_count = 0
        
        for i, file_info in enumerate(missing_files, 1):
            url = file_info['url']
            session_type = file_info['session_type']
            identifier = file_info['identifier']
            session_id_for_update = file_info['session_id']
            
            print(f"[{i}/{len(missing_files)}] Re-downloading from {session_type}/{identifier}...")
            
            try:
                # Download the media
                file_path = self.downloader.download_media(
                    url=url,
                    scrape_type=session_type,
                    identifier=identifier,
                    referer=f"https://www.lemon8-app.com/"
                )
                
                if file_path:
                    downloaded_count += 1
                    print(f"✅ Re-downloaded successfully: {os.path.basename(file_path)}")
                else:
                    failed_count += 1
                    print(f"❌ Re-download failed")
            
            except Exception as e:
                print(f"❌ Error re-downloading: {e}")
                failed_count += 1
        
        # Print summary
        print(f"\n{'=' * 50}")
        print(f"📊 Reconciliation Summary:")
        print(f"   ✅ Re-downloaded: {downloaded_count}")
        print(f"   ❌ Failed: {failed_count}")
        print(f"   📈 Total processed: {len(missing_files)}")

        # Save progress
        self.save()

        # Second pass: export any profile photo blobs that were stored in DB
        # but never written to disk (complementary to the session-ledger check above)
        from reconciler import Reconciler
        rec = Reconciler()
        try:
            blob_stats = rec.reconcile_profile_photos()
            if blob_stats['total'] > 0:
                print(f"\n📸 Profile Photo Blob Export:")
                print(f"   Total blobs in DB: {blob_stats['total']}")
                print(f"   Exported to disk:  {blob_stats['exported']}")
                print(f"   Already on disk:   {blob_stats['skipped']}")
        finally:
            rec.close()
    
    def view_user_history(self, username: str, limit: int = 10):
        """View user follower/following history"""
        username = username.lstrip('@').lower()
        
        print(f"\n📜 User History for @{username}")
        print("=" * 50)
        
        # Get user history from AccountTracker
        history = self.tracker.account_tracker.get_user_history(username, limit)
        
        if not history:
            print(f"ℹ️ No history found for @{username}")
            print(f"💡 Tip: History is created when scraping user profiles")
            return
        
        # Display history
        print(f"\n📊 Found {len(history)} snapshot(s):\n")
        
        for i, snapshot in enumerate(history, 1):
            timestamp = snapshot.get('snapshot_ts', 'Unknown')
            followers = snapshot.get('followers_count', 0)
            following = snapshot.get('following_count', 0)
            posts = snapshot.get('post_count', 0)
            user_id = snapshot.get('user_id', 'N/A')
            
            print(f"{i}. {timestamp}")
            print(f"   👥 Followers: {followers:,}")
            print(f"   ➡️ Following: {following:,}")
            print(f"   📝 Posts: {posts:,}")
            print(f"   🆔 User ID: {user_id}")
            print()
    
    def view_photo_history(self, username: str, limit: int = 10):
        """View profile photo change history"""
        username = username.lstrip('@').lower()
        
        print(f"\n📸 Profile Photo History for @{username}")
        print("=" * 50)
        
        # Import ProfilePhotoTracker
        from profile_photo_tracker import ProfilePhotoTracker
        
        # Get photo history
        photo_tracker = ProfilePhotoTracker()
        try:
            history = photo_tracker.get_photo_history(username, limit)
            
            if not history:
                print(f"ℹ️ No photo history found for @{username}")
                print(f"💡 Tip: Photo history is tracked when scraping user profiles with profile photo tracking enabled")
                return
            
            # Display history
            print(f"\n📊 Found {len(history)} photo change(s):\n")
            
            for i, photo in enumerate(history, 1):
                timestamp = photo.get('detected_at', 'Unknown')
                photo_url = photo.get('photo_url', 'N/A')
                phash = photo.get('photo_phash', 'N/A')
                user_id = photo.get('user_id', 'N/A')
                file_path = photo.get('file_path', 'N/A')
                
                print(f"{i}. {timestamp}")
                print(f"   🔗 URL: {photo_url}")
                print(f"   🔢 pHash: {phash}")
                print(f"   🆔 User ID: {user_id}")
                if file_path and file_path != 'N/A':
                    print(f"   📁 File: {file_path}")
                print()
        finally:
            photo_tracker.close()

    # ── Account Management ──────────────────────────────────────────────────

    def list_accounts(self):
        """List all configured accounts with status and cooldown info"""
        from account_manager import AccountManager
        mgr = AccountManager()
        try:
            accounts = mgr.get_all_accounts()
            print("\n👤 Configured Accounts")
            print("=" * 60)
            if not accounts:
                print("ℹ️ No accounts configured.")
                print("💡 Tip: Use `python main.py accounts add <name> <cookies_file>` to add one.")
                return
            now_str = datetime.now().isoformat()
            for acc in accounts:
                name = acc['account_name']
                active = "✅ Active" if acc.get('is_active') else "❌ Inactive"
                last_used = acc.get('last_used_ts') or 'Never'
                cooldown_until = acc.get('cooldown_until')
                if cooldown_until and cooldown_until > now_str:
                    status = f"⏳ Cooldown until {cooldown_until}"
                else:
                    status = active
                print(f"  • {name}  [{status}]  Last used: {last_used}")
            stats = mgr.get_account_stats()
            print(f"\n📊 Total: {stats['total_accounts']}  Available: {stats['available']}  In cooldown: {stats['in_cooldown']}")
        finally:
            mgr.close()

    def add_account(self, name: str, cookies_file: str):
        """Add a new account to the pool"""
        from account_manager import AccountManager
        mgr = AccountManager()
        try:
            mgr.add_account(name, cookies_file)
        finally:
            mgr.close()

    def view_cooldowns(self):
        """Display accounts currently in cooldown with time remaining"""
        from account_manager import AccountManager
        from datetime import timezone
        mgr = AccountManager()
        try:
            accounts = mgr.get_all_accounts()
            now_str = datetime.now().isoformat()
            in_cooldown = [a for a in accounts if a.get('cooldown_until') and a['cooldown_until'] > now_str]
            print("\n⏳ Account Cooldowns")
            print("=" * 60)
            if not in_cooldown:
                print("✅ No accounts are currently in cooldown.")
                return
            for acc in in_cooldown:
                name = acc['account_name']
                until = acc['cooldown_until']
                reason = acc.get('cooldown_reason', 'unknown')
                print(f"  • {name}  Cooldown until: {until}  Reason: {reason}")
        finally:
            mgr.close()

    def test_accounts(self):
        """Test all accounts by making a lightweight API request"""
        from account_manager import AccountManager
        import requests
        mgr = AccountManager()
        try:
            accounts = mgr.get_all_accounts()
            print("\n🧪 Testing Accounts")
            print("=" * 60)
            if not accounts:
                print("ℹ️ No accounts configured.")
                return
            passed = 0
            failed = 0
            for acc in accounts:
                name = acc['account_name']
                cookies_path = acc.get('cookies_file_path', '')
                if not os.path.exists(cookies_path):
                    print(f"  ❌ {name}  — cookies file missing: {cookies_path}")
                    failed += 1
                    continue
                # Load cookies from file and make a simple request
                try:
                    session = requests.Session()
                    with open(cookies_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                parts = line.split('\t')
                                if len(parts) >= 7:
                                    session.cookies.set(parts[5], parts[6], domain=parts[0])
                    resp = session.get('https://www.lemon8-app.com/', timeout=10, allow_redirects=True)
                    if resp.status_code < 400:
                        print(f"  ✅ {name}  — OK (HTTP {resp.status_code})")
                        passed += 1
                    else:
                        print(f"  ❌ {name}  — HTTP {resp.status_code}")
                        failed += 1
                except Exception as e:
                    print(f"  ❌ {name}  — Error: {e}")
                    failed += 1
            print(f"\n📊 Results: {passed} passed, {failed} failed")
        finally:
            mgr.close()

    # ── Database Operations ─────────────────────────────────────────────────

    def view_recent_sessions(self, limit: int = 20):
        """Display recent scraping sessions in a table"""
        print(f"\n📋 Recent Sessions (last {limit})")
        print("=" * 70)
        sessions = self.progress.get_all_sessions_summary()
        if not sessions:
            print("ℹ️ No sessions found.")
            return
        # Most recent first, capped at limit
        sessions = list(reversed(sessions))[:limit]
        for s in sessions:
            sid = s.get('session_id', '')[:40]
            stype = s.get('session_type', '')
            ident = s.get('identifier', '')
            status = s.get('status', '')
            scraped = s.get('total_scraped', 0)
            downloaded = s.get('total_downloaded', 0)
            start = (s.get('start_time') or '')[:19]
            status_icon = {'completed': '✅', 'failed': '❌', 'in_progress': '⏳', 'cancelled': '🛑'}.get(status, '❓')
            print(f"  {status_icon} [{stype}] {ident}  scraped:{scraped} dl:{downloaded}  {start}")
        print(f"\n📊 Showing {len(sessions)} of {len(self.progress.get_all_sessions_summary())} total sessions")

    def backup_database(self, output_dir: Optional[str] = None):
        """Create a timestamped backup of lemon8_toolkit.db"""
        import shutil
        from config import LEMON8_DB_FILE
        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(LEMON8_DB_FILE), '..', 'backups')
        output_dir = os.path.abspath(output_dir)
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"lemon8_toolkit_{ts}.db"
        backup_path = os.path.join(output_dir, backup_name)
        shutil.copy2(LEMON8_DB_FILE, backup_path)
        size_kb = os.path.getsize(backup_path) // 1024
        print(f"\n💾 Database Backup")
        print("=" * 50)
        print(f"✅ Backup created: {backup_path}")
        print(f"📦 Size: {size_kb} KB")

    def show_blob_stats(self):
        """Display blob storage statistics from ProfilePhotoTracker"""
        from profile_photo_tracker import ProfilePhotoTracker
        tracker = ProfilePhotoTracker()
        try:
            print("\n🗄️ Blob Storage Statistics")
            print("=" * 50)
            try:
                stats = tracker.get_stats()
                for key, val in stats.items():
                    print(f"  {key}: {val}")
            except AttributeError:
                # Fallback: query the table directly
                cursor = tracker.conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM profile_photo_history")
                count = cursor.fetchone()[0]
                print(f"  Total profile photo records: {count}")
        finally:
            tracker.close()

    # ── System Utilities ────────────────────────────────────────────────────

    def clear_cache(self):
        """Reset in-progress sessions and stuck spiders"""
        print("\n🧹 Clearing Session Cache")
        print("=" * 50)
        # Reset stuck spiders
        reset_count = self.tracker.account_tracker.reset_stuck_spiders()
        # Reset in-progress sessions
        sessions = self.progress.get_all_sessions_summary()
        cleared_sessions = 0
        for s in sessions:
            if s.get('status') == 'in_progress':
                self.progress.end_session(s['session_id'], 'cancelled')
                cleared_sessions += 1
        self.save()
        print(f"✅ Reset {reset_count} stuck spider(s)")
        print(f"✅ Cancelled {cleared_sessions} in-progress session(s)")
        print("✅ Cache cleared successfully")


def main():
    # SIGTERM (taskkill) and SIGBREAK (bat window X button) → sys.exit triggers atexit WAL checkpoint
    def _shutdown_signal(signum, frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown_signal)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, _shutdown_signal)

    def resolve_profile_photo_override(args: argparse.Namespace) -> Optional[bool]:
        if getattr(args, 'include_profile_photos', False):
            return True
        if getattr(args, 'exclude_profile_photos', False):
            return False
        return None

    def build_force_user_targets(toolkit: Lemon8Toolkit, target_username: Optional[str]) -> List[str]:
        """
        Build a deterministic force-rerun queue from tracked DB users.
        If a target username is provided, it is prioritized first.
        """
        tracked_users = toolkit.tracker.account_tracker.get_all_visited_users()

        ordered_targets: List[str] = []
        seen = set()

        if target_username:
            normalized_target = target_username.lstrip('@').lower()
            if normalized_target:
                ordered_targets.append(normalized_target)
                seen.add(normalized_target)

        for username in tracked_users:
            normalized_username = str(username).lstrip('@').lower()
            if normalized_username and normalized_username not in seen:
                ordered_targets.append(normalized_username)
                seen.add(normalized_username)

        return ordered_targets

    parser = argparse.ArgumentParser(
        description="🚀 Unified Lemon8 Toolkit - Professional Media Scraper & Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
🌟 Features:
  - 👤 User Profile: Scrape all media from any @username
  - 📱 For You Feed: Discover and download trending content
  - 🏷️ Tag/Topic: Scrape specific niche topics by ID
  - 📊 Statistics: Track your scraping progress and history
  - 🧹 Data Management: Easily clear tracking and download history

💡 Usage Examples:
  python main.py user walshdelaney --download
  python main.py feed --pages 5 --download --out "C:\\Downloads\\Lemon8"
    python main.py tag 7549513626407780359 --pages 5 --download --force
    python main.py tag singapore --pages 3 --download
  python main.py stats
  python main.py clear
        """
    )
    
    # Global options
    parser.add_argument(
        '--out',
        dest='global_out',
        help='Specify custom download directory (can be placed before the subcommand)',
    )
    
    subparsers = parser.add_subparsers(dest='mode', help='Available modes', required=True)
    
    # User scrape
    user_parser = subparsers.add_parser('user', help='👤 Scrape media from a user profile')
    user_parser.add_argument('username', nargs='?', help='Target username (with or without @). Leave empty to pick from discovered list.')
    user_parser.add_argument('--userid', dest='user_id', help='Target user by numeric user_id instead of username')
    user_parser.add_argument('--download', action='store_true', help='Enable media downloading')
    user_parser.add_argument(
        '--force',
        action='store_true',
        help='Force rescrape and rerun all tracked users in DB (target username runs first if provided)',
    )
    user_parser.add_argument(
        '--out',
        dest='mode_out',
        help='Specify custom download directory (can also be provided before the subcommand)',
    )
    user_profile_group = user_parser.add_mutually_exclusive_group()
    user_profile_group.add_argument(
        '--include-profile-photos',
        action='store_true',
        help='Include profile-photo media for this run',
    )
    user_profile_group.add_argument(
        '--exclude-profile-photos',
        action='store_true',
        help='Skip profile-photo media for this run',
    )
    
    # Feed scrape
    feed_parser = subparsers.add_parser('feed', help='📱 Scrape trending media from For You feed')
    feed_parser.add_argument('--pages', type=int, default=10, help='Number of feed pages to scrape (default: 10)')
    feed_parser.add_argument('--download', action='store_true', help='Enable media downloading')
    feed_parser.add_argument(
        '--out',
        dest='mode_out',
        help='Specify custom download directory (can also be provided before the subcommand)',
    )
    feed_profile_group = feed_parser.add_mutually_exclusive_group()
    feed_profile_group.add_argument(
        '--include-profile-photos',
        action='store_true',
        help='Include author profile photos for this run',
    )
    feed_profile_group.add_argument(
        '--exclude-profile-photos',
        action='store_true',
        help='Skip author profile photos for this run',
    )
    
    # Tag scrape
    tag_parser = subparsers.add_parser('tag', help='🏷️ Scrape media from a tag/topic ID or keyword')
    tag_parser.add_argument('tag_id', help='Target tag/topic numeric ID or keyword (e.g., 754951... or singapore)')
    tag_parser.add_argument('--pages', type=int, default=10, help='Number of tag pages to scrape (default: 10)')
    tag_parser.add_argument('--download', action='store_true', help='Enable media downloading')
    tag_parser.add_argument('--force', action='store_true', help='Force rescrape even if already processed')
    tag_parser.add_argument(
        '--out',
        dest='mode_out',
        help='Specify custom download directory (can also be provided before the subcommand)',
    )
    
    # Seed from feed
    seed_parser = subparsers.add_parser('seed', help='🌱 Seed spider queue from For You feed')
    seed_parser.add_argument('--pages', type=int, default=10, help='Number of feed pages to scrape (default: 10)')
    seed_parser.add_argument('--download', action='store_true', help='Enable media downloading during seed')
    
    # Spider batch
    spider_parser = subparsers.add_parser('spider', help='🕷️ Spider a batch of pending users')
    spider_parser.add_argument('--batch', type=int, default=10, help='Number of users to spider (default: 10)')
    spider_parser.add_argument('--download', action='store_true', help='Enable media downloading')
    spider_parser.add_argument(
        '--out',
        dest='mode_out',
        help='Specify custom download directory (can also be provided before the subcommand)',
    )
    
    # Graph commands
    graph_parser = subparsers.add_parser('graph', help='🕸️ Network graph operations')
    graph_subparsers = graph_parser.add_subparsers(dest='graph_action', help='Graph actions', required=True)
    
    # Build graph
    build_graph_parser = graph_subparsers.add_parser('build', help='Build graph from tracked users')
    build_graph_parser.add_argument('--limit', type=int, help='Limit number of users to process')
    
    # Show graph stats
    graph_subparsers.add_parser('stats', help='Show graph statistics')
    
    # Download management commands
    download_pending_parser = subparsers.add_parser('download-pending', help='⬇️ Download pending media from progress database')
    download_pending_parser.add_argument('--limit', type=int, default=100, help='Limit number of media to download (default: 100)')
    
    reconcile_parser = subparsers.add_parser('reconcile', help='🔄 Reconcile missing files and re-download')
    reconcile_parser.add_argument('--session', help='Reconcile specific session ID (optional)')
    
    # History commands
    history_parser = subparsers.add_parser('history', help='📜 View user history')
    history_subparsers = history_parser.add_subparsers(dest='history_action', help='History actions', required=True)
    
    # User history
    user_history_parser = history_subparsers.add_parser('user', help='View follower/following history')
    user_history_parser.add_argument('username', help='Username to view history for')
    user_history_parser.add_argument('--limit', type=int, default=10, help='Number of snapshots to show (default: 10)')
    
    # Photo history
    photo_history_parser = history_subparsers.add_parser('photo', help='View profile photo change history')
    photo_history_parser.add_argument('username', help='Username to view photo history for')
    photo_history_parser.add_argument('--limit', type=int, default=10, help='Number of photos to show (default: 10)')
    
    # Account management commands
    accounts_parser = subparsers.add_parser('accounts', help='👤 Manage account cookies')
    accounts_subparsers = accounts_parser.add_subparsers(dest='accounts_action', help='Account actions', required=True)
    accounts_subparsers.add_parser('list', help='List all configured accounts')
    add_account_parser = accounts_subparsers.add_parser('add', help='Add a new account')
    add_account_parser.add_argument('name', help='Account name')
    add_account_parser.add_argument('cookies_file', help='Path to cookies.txt file')
    accounts_subparsers.add_parser('cooldowns', help='View accounts in cooldown')
    accounts_subparsers.add_parser('test', help='Test all account cookies')

    # Database operations commands
    sessions_parser = subparsers.add_parser('sessions', help='📋 View recent scraping sessions')
    sessions_parser.add_argument('--limit', type=int, default=20, help='Number of sessions to show (default: 20)')

    backup_parser = subparsers.add_parser('backup', help='💾 Backup the database')
    backup_parser.add_argument('--output', help='Custom backup directory (optional)')

    blobs_parser = subparsers.add_parser('blobs', help='🗄️ Manage blob storage')
    blobs_subparsers = blobs_parser.add_subparsers(dest='blobs_action', help='Blob actions', required=True)
    blobs_subparsers.add_parser('stats', help='Show blob storage statistics')
    blobs_subparsers.add_parser('export', help='Export blob to file (coming soon)')
    blobs_subparsers.add_parser('cleanup', help='Clean up old blobs (coming soon)')

    # System utilities commands
    subparsers.add_parser('cache', help='🧹 Clear in-progress session cache and stuck spiders')

    # Stats
    subparsers.add_parser('stats', help='📊 View toolkit usage statistics and history')
    
    # Clear
    subparsers.add_parser('clear', help='🧹 Reset tracking, progress, and download history')
    
    args = parser.parse_args()
    
    # Initialize toolkit
    try:
        # Use auto_save=False for batch operations to improve performance
        toolkit = Lemon8Toolkit(auto_save=False)
        
        # Resolve --out from either global or subcommand position (subcommand wins)
        resolved_out = getattr(args, 'mode_out', None) or getattr(args, 'global_out', None)

        # Always resolve download destination before any scraping/fetching begins
        # when download mode is requested.
        needs_download_path = (
            args.mode in {'user', 'feed', 'tag'} and
            bool(getattr(args, 'download', False))
        )
        if needs_download_path:
            if resolved_out:
                # Explicit --out path still takes precedence.
                prompt_for_download_path(context="Lemon8 media", out_path=resolved_out)
            else:
                # Force an explicit custom-path prompt before scraping starts.
                prompt_for_download_path(
                    context="Lemon8 media",
                    allow_session_reuse=False,
                    default_path=None,
                )
            
    except Exception as e:
        print(f"❌ Failed to initialize toolkit: {e}")
        return
    
    # Execute command
    try:
        if args.mode == 'user':
            if args.force:
                force_targets = build_force_user_targets(toolkit, args.username)

                if force_targets:
                    print(f"\n🔁 Force rerun enabled: processing {len(force_targets)} tracked users from DB")
                    for index, username in enumerate(force_targets, 1):
                        print(f"\n▶️ [{index}/{len(force_targets)}] Processing @{username}")
                        toolkit.scrape_user(
                            username,
                            None,  # user_id
                            args.download,
                            True,
                            include_profile_photos=resolve_profile_photo_override(args),
                        )
                else:
                    toolkit.scrape_user(
                        args.username,
                        getattr(args, 'user_id', None),
                        args.download,
                        True,
                        include_profile_photos=resolve_profile_photo_override(args),
                    )
            else:
                toolkit.scrape_user(
                    args.username,
                    getattr(args, 'user_id', None),
                    args.download,
                    False,
                    include_profile_photos=resolve_profile_photo_override(args),
                )
        elif args.mode == 'feed':
            toolkit.scrape_feed(
                args.pages,
                args.download,
                include_profile_photos=resolve_profile_photo_override(args),
            )
        elif args.mode == 'tag':
            toolkit.scrape_tag(args.tag_id, args.download, args.force, pages=args.pages)
        elif args.mode == 'seed':
            toolkit.seed_from_feed(args.pages, args.download)
        elif args.mode == 'spider':
            toolkit.spider_batch(args.batch, args.download)
        elif args.mode == 'graph':
            if args.graph_action == 'build':
                toolkit.build_graph(args.limit)
            elif args.graph_action == 'stats':
                toolkit.show_graph_stats()
        elif args.mode == 'download-pending':
            toolkit.download_pending_media(args.limit)
        elif args.mode == 'reconcile':
            toolkit.reconcile_missing_files(getattr(args, 'session', None))
        elif args.mode == 'history':
            if args.history_action == 'user':
                toolkit.view_user_history(args.username, args.limit)
            elif args.history_action == 'photo':
                toolkit.view_photo_history(args.username, args.limit)
        elif args.mode == 'accounts':
            if args.accounts_action == 'list':
                toolkit.list_accounts()
            elif args.accounts_action == 'add':
                toolkit.add_account(args.name, args.cookies_file)
            elif args.accounts_action == 'cooldowns':
                toolkit.view_cooldowns()
            elif args.accounts_action == 'test':
                toolkit.test_accounts()
        elif args.mode == 'sessions':
            toolkit.view_recent_sessions(args.limit)
        elif args.mode == 'backup':
            toolkit.backup_database(getattr(args, 'output', None))
        elif args.mode == 'blobs':
            if args.blobs_action == 'stats':
                toolkit.show_blob_stats()
            else:
                print(f"ℹ️ blobs {args.blobs_action} — coming soon")
        elif args.mode == 'cache':
            toolkit.clear_cache()
        elif args.mode == 'stats':
            toolkit.show_stats()
        elif args.mode == 'clear':
            toolkit.clear_all()
        else:
            parser.print_help()
        
        # Save all buffered data after completion
        toolkit.save()
    
    except KeyboardInterrupt:
        print("\n\n🛑 Operation cancelled by user")
        current_session = toolkit.progress.get_current_session()
        if current_session:
            toolkit.progress.end_session(current_session['session_id'], 'cancelled')
        toolkit.save()
        # Clean up any orphaned .tmp download files
        downloads_dir = toolkit.downloader.downloads_dir or ""
        if downloads_dir and os.path.isdir(downloads_dir):
            for tmp_file in _glob.glob(os.path.join(downloads_dir, "**", "*.tmp"), recursive=True):
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        # Try to end current session as failed
        current_session = toolkit.progress.get_current_session()
        if current_session:
            toolkit.progress.end_session(current_session['session_id'], 'failed')
        toolkit.save()


if __name__ == "__main__":
    main()
