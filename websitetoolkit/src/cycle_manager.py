"""
Unified Website Toolkit - Cycle Manager
Orchestrates the discovery → scraping → repeat cycle with intelligent rate limiting
"""
import asyncio
import time
import json
import os
from logger_config import setup_logger
from db_manager import get_db_manager
from resilience import _SHUTDOWN, _interruptible_sleep, wait_for_internet

logger = setup_logger(__name__)

from datetime import datetime, timedelta
from typing import Dict, List, Set, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor
import random

try:
    from config import get_config, get_enabled_websites
    from link_spider import LinkSpider
    from photo_scraper import PhotoScraper
    from utils import ProgressTracker
    from download_helper import prompt_for_download_location
    MODULES_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Warning: Some modules not available: {e}")
    MODULES_AVAILABLE = False


def load_cycle_config() -> Dict[str, Any]:
    """Load cycle configuration from automation folder"""
    config_path = os.path.join("data", "automation", "cycle_config.json")
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"WARNING: Error loading cycle config: {e}")
    
    # Return default configuration if file doesn't exist
    return {
        "cycle_settings": {
            "default_max_cycles": 1,
            "default_concurrent_websites": 3,
            "default_chunk_size": 5
        },
        "rate_limiting": {
            "requests_per_minute": 20,
            "requests_per_hour": 800,
            "requests_per_day": 5000,
            "min_delay_between_requests": 3.0,
            "max_delay_between_requests": 10.0
        },
        "safety_limits": {
            "max_concurrent_websites": 10,
            "emergency_stop_on_errors": 10
        }
    }


@dataclass
class CycleStats:
    """Statistics for a complete cycle"""
    cycle_id: str
    start_time: str
    end_time: Optional[str] = None
    websites_crawled: int = 0
    websites_scraped: int = 0
    links_discovered: int = 0
    new_websites_added: int = 0
    photos_downloaded: int = 0
    total_errors: int = 0
    cycle_duration_seconds: float = 0.0


@dataclass
class RateLimitConfig:
    """Rate limiting configuration"""
    requests_per_minute: int = 30
    requests_per_hour: int = 1000
    requests_per_day: int = 10000
    min_delay_between_requests: float = 2.0
    max_delay_between_requests: float = 5.0
    backoff_multiplier: float = 1.5
    max_retries: int = 3


class GlobalRateLimiter:
    """Centralized rate limiter to prevent IP bans across all operations"""
    
    def __init__(self, config: RateLimitConfig):
        self.config = config
        self.request_times: List[float] = []
        self.hourly_requests: List[float] = []
        self.daily_requests: List[float] = []
        self.last_request_time: float = 0
        self.current_delay: float = config.min_delay_between_requests
        self.lock = asyncio.Lock()
    
    async def acquire(self) -> bool:
        """Acquire permission to make a request"""
        async with self.lock:
            now = time.time()
            
            # Clean old timestamps
            self._clean_old_timestamps(now)
            
            # Check if we're within limits
            if not self._check_limits(now):
                return False
            
            # Calculate delay
            delay = self._calculate_delay(now)
            if delay > 0:
                await asyncio.sleep(delay)
            
            # Record request
            self.request_times.append(time.time())
            self.hourly_requests.append(time.time())
            self.daily_requests.append(time.time())
            self.last_request_time = time.time()
            
            return True
    
    def _clean_old_timestamps(self, now: float):
        """Remove timestamps outside our tracking windows"""
        minute_ago = now - 60
        hour_ago = now - 3600
        day_ago = now - 86400
        
        self.request_times = [t for t in self.request_times if t > minute_ago]
        self.hourly_requests = [t for t in self.hourly_requests if t > hour_ago]
        self.daily_requests = [t for t in self.daily_requests if t > day_ago]
    
    def _check_limits(self, now: float) -> bool:
        """Check if we're within rate limits"""
        return (
            len(self.request_times) < self.config.requests_per_minute and
            len(self.hourly_requests) < self.config.requests_per_hour and
            len(self.daily_requests) < self.config.requests_per_day
        )
    
    def _calculate_delay(self, now: float) -> float:
        """Calculate delay needed before next request"""
        if self.last_request_time == 0:
            return 0
        
        time_since_last = now - self.last_request_time
        min_delay = self.config.min_delay_between_requests
        
        if time_since_last < min_delay:
            base_delay = min_delay - time_since_last
            jitter = random.uniform(0, 1) * base_delay * 0.2
            return base_delay + jitter
        
        return 0
    
    def record_error(self):
        """Record an error and increase delay"""
        self.current_delay = min(
            self.current_delay * self.config.backoff_multiplier,
            self.config.max_delay_between_requests
        )
    
    def record_success(self):
        """Record a success and potentially decrease delay"""
        self.current_delay = max(
            self.current_delay / self.config.backoff_multiplier,
            self.config.min_delay_between_requests
        )


class CycleManager:
    """Main cycle orchestrator"""

    def __init__(self,
                 rate_limit_config: Optional[RateLimitConfig] = None,
                 max_concurrent_websites: int = 3,
                 chunk_size: int = 10,
                 custom_download_dir: Optional[str] = None):
        self.rate_limiter = GlobalRateLimiter(rate_limit_config or RateLimitConfig())
        self.max_concurrent_websites = max_concurrent_websites
        self.chunk_size = chunk_size
        self.custom_download_dir = custom_download_dir
        self.current_cycle: Optional[CycleStats] = None
        self.is_running = False
        self._cycle_data_dir = os.path.join("data", "cycles")
        self._chunks_dir = os.path.join("data", "chunks")
        os.makedirs(self._cycle_data_dir, exist_ok=True)
        os.makedirs(self._chunks_dir, exist_ok=True)

    def _save_cycle_stats(self, stats: CycleStats):
        cycle_file = os.path.join(self._cycle_data_dir, f"cycle_{stats.cycle_id}.json")
        with open(cycle_file, 'w', encoding='utf-8') as f:
            json.dump(asdict(stats), f, indent=2, ensure_ascii=False)

    def _get_recent_cycles(self, limit: int = 10) -> List[CycleStats]:
        cycles = []
        try:
            cycle_files = sorted(
                [f for f in os.listdir(self._cycle_data_dir) if f.startswith("cycle_")],
                reverse=True
            )
            for cycle_file in cycle_files[:limit]:
                cycle_path = os.path.join(self._cycle_data_dir, cycle_file)
                with open(cycle_path, 'r', encoding='utf-8') as f:
                    cycles.append(CycleStats(**json.load(f)))
        except Exception as e:
            logger.warning("Error loading cycle data: %s", e)
        return cycles

    def _chunk_websites(self, websites: List[Dict], chunk_size: int = 10) -> List[List[Dict]]:
        return [websites[i:i + chunk_size] for i in range(0, len(websites), chunk_size)]

    def _save_chunk_progress(self, cycle_id: str, chunk_id: int, progress_data: Dict):
        chunk_file = os.path.join(self._chunks_dir, f"cycle_{cycle_id}_chunk_{chunk_id}.json")
        with open(chunk_file, 'w', encoding='utf-8') as f:
            json.dump(progress_data, f, indent=2, ensure_ascii=False)
        
    def generate_cycle_id(self) -> str:
        """Generate unique cycle ID"""
        return datetime.now().strftime("%Y%m%d_%H%M%S")
    
    async def run_discovery_phase(self, websites: List[Dict], cycle_id: str) -> Tuple[int, int]:
        """Run link discovery phase"""
        logger.info("\n🔍 DISCOVERY PHASE: Starting link crawling...")
        
        semaphore = asyncio.Semaphore(self.max_concurrent_websites)
        chunks = self._chunk_websites(websites, self.chunk_size)
        
        total_links = 0
        total_new_websites = 0
        
        for chunk_id, chunk in enumerate(chunks):
            logger.info(f"CHUNK: Processing chunk {chunk_id + 1}/{len(chunks)} ({len(chunk)} websites)")
            
            # Process chunk with controlled concurrency
            tasks = []
            for website in chunk:
                task = self._crawl_website_with_limits(website, semaphore, cycle_id, chunk_id)
                tasks.append(task)
            
            # Execute chunk with progress tracking
            chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            for i, result in enumerate(chunk_results):
                if isinstance(result, Exception):
                    logger.error(f"ERROR: Chunk {chunk_id}, Website {i}: {result}")
                    continue
                
                if isinstance(result, dict):
                    total_links += result.get('total_links_found', 0)
                    total_new_websites += result.get('websites_added_to_config', 0)
            
            # Save chunk progress
            chunk_progress = {
                'chunk_id': chunk_id,
                'websites_processed': len(chunk),
                'links_found': sum(r.get('total_links_found', 0) for r in chunk_results if isinstance(r, dict)),
                'new_websites': sum(r.get('websites_added_to_config', 0) for r in chunk_results if isinstance(r, dict)),
                'timestamp': datetime.now().isoformat()
            }
            self._save_chunk_progress(cycle_id, chunk_id, chunk_progress)
            
            logger.info(f"CHUNK COMPLETE: {chunk_progress['links_found']} links, {chunk_progress['new_websites']} new websites")
        
        return total_links, total_new_websites
    
    async def _crawl_website_with_limits(self, website: Dict, semaphore: asyncio.Semaphore, 
                                        cycle_id: str, chunk_id: int) -> Dict:
        """Crawl a single website with rate limiting"""
        async with semaphore:
            website_name = website.get('name', 'unknown')
            website_url = website.get('url', '')
            
            if not website_url:
                return {'error': 'No URL configured'}
            
            try:
                # Acquire rate limit permission
                if not await self.rate_limiter.acquire():
                    logger.info(f"RATE LIMIT: Skipping {website_name} due to rate limits")
                    return {'error': 'Rate limited'}
                
                logger.info(f"CRAWLING: {website_name}")
                
                # Create spider and crawl
                spider = LinkSpider(website_name)
                result = await spider.crawl_website_urls(
                    [website_url], 
                    auto_add_websites=True, 
                    auto_enable_websites=False
                )
                
                self.rate_limiter.record_success()
                return result
                
            except Exception as e:
                self.rate_limiter.record_error()
                logger.error(f"ERROR: Failed to crawl {website_name}: {e}")
                return {'error': str(e)}
    
    async def run_scraping_phase(self, websites: List[Dict], cycle_id: str) -> int:
        """Run photo scraping phase"""
        logger.info("\n📸 SCRAPING PHASE: Starting photo downloads...")
        
        semaphore = asyncio.Semaphore(self.max_concurrent_websites)
        chunks = self._chunk_websites(websites, self.chunk_size)
        
        total_photos = 0
        
        for chunk_id, chunk in enumerate(chunks):
            logger.info(f"CHUNK: Scraping chunk {chunk_id + 1}/{len(chunks)} ({len(chunk)} websites)")
            
            # Process chunk with controlled concurrency
            tasks = []
            for website in chunk:
                task = self._scrape_website_with_limits(website, semaphore, cycle_id, chunk_id)
                tasks.append(task)
            
            # Execute chunk
            chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            chunk_photos = 0
            for i, result in enumerate(chunk_results):
                if isinstance(result, Exception):
                    logger.error(f"ERROR: Chunk {chunk_id}, Website {i}: {result}")
                    continue
                
                if isinstance(result, dict):
                    photos = result.get('total_images_downloaded', 0)
                    chunk_photos += photos
                    total_photos += photos
            
            logger.info(f"CHUNK COMPLETE: {chunk_photos} photos downloaded")
        
        return total_photos
    
    async def _scrape_website_with_limits(self, website: Dict, semaphore: asyncio.Semaphore,
                                         cycle_id: str, chunk_id: int) -> Dict:
        """Scrape a single website with rate limiting"""
        async with semaphore:
            website_name = website.get('name', 'unknown')
            website_url = website.get('url', '')
            
            if not website_url:
                return {'error': 'No URL configured'}
            
            try:
                # Acquire rate limit permission
                if not await self.rate_limiter.acquire():
                    logger.info(f"RATE LIMIT: Skipping scraping {website_name} due to rate limits")
                    return {'error': 'Rate limited'}
                
                logger.info(f"SCRAPING: {website_name}")
                
                # Create scraper with custom download directory and scrape
                scraper = PhotoScraper(website_name, self.custom_download_dir, website_url)
                result = await scraper.scrape_website_images([website_url])
                
                self.rate_limiter.record_success()
                return result
                
            except Exception as e:
                self.rate_limiter.record_error()
                logger.error(f"ERROR: Failed to scrape {website_name}: {e}")
                return {'error': str(e)}
    
    async def run_cycle(self, max_cycles: int = 1, discovery_enabled: bool = True,
                       scraping_enabled: bool = True) -> CycleStats:
        if not MODULES_AVAILABLE:
            raise RuntimeError(
                "CycleManager cannot run: required modules failed to import at startup. "
                "Check logs for the ImportError."
            )
        if self.is_running:
            raise RuntimeError("Cycle already running")
        
        self.is_running = True
        cycle_id = self.generate_cycle_id()
        
        # Initialize cycle stats
        self.current_cycle = CycleStats(
            cycle_id=cycle_id,
            start_time=datetime.now().isoformat()
        )
        
        try:
            logger.info(f"\n🚀 STARTING CYCLE {cycle_id}")
            logger.info("=" * 50)
            
            for cycle_num in range(max_cycles):
                # Check for shutdown
                if _SHUTDOWN.is_set():
                    logger.info("[STOPPED] Shutdown requested, stopping cycle")
                    break
                
                logger.info(f"\n📋 CYCLE {cycle_num + 1}/{max_cycles}")
                
                # Get current websites
                websites = get_enabled_websites()
                if not websites:
                    logger.warning("WARNING: No enabled websites found")
                    break
                
                logger.info(f"WEBSITES: Processing {len(websites)} enabled websites")
                
                # Discovery phase
                if discovery_enabled:
                    # Check for shutdown before discovery
                    if _SHUTDOWN.is_set():
                        logger.info("[STOPPED] Shutdown requested before discovery phase")
                        break
                    
                    # Wait for internet if needed
                    if not wait_for_internet():
                        logger.info("[STOPPED] Shutdown requested while waiting for internet")
                        break
                    
                    links_found, new_websites = await self.run_discovery_phase(websites, cycle_id)
                    self.current_cycle.websites_crawled += len(websites)
                    self.current_cycle.links_discovered += links_found
                    self.current_cycle.new_websites_added += new_websites
                    
                    logger.info(f"DISCOVERY COMPLETE: {links_found} links, {new_websites} new websites")
                    
                    # Refresh website list after discovery
                    websites = get_enabled_websites()
                
                # Scraping phase
                if scraping_enabled:
                    # Check for shutdown before scraping
                    if _SHUTDOWN.is_set():
                        logger.info("[STOPPED] Shutdown requested before scraping phase")
                        break
                    
                    # Wait for internet if needed
                    if not wait_for_internet():
                        logger.info("[STOPPED] Shutdown requested while waiting for internet")
                        break
                    
                    photos_downloaded = await self.run_scraping_phase(websites, cycle_id)
                    self.current_cycle.websites_scraped += len(websites)
                    self.current_cycle.photos_downloaded += photos_downloaded
                    
                    logger.info(f"SCRAPING COMPLETE: {photos_downloaded} photos downloaded")
            
            # Finalize cycle
            self.current_cycle.end_time = datetime.now().isoformat()
            start_dt = datetime.fromisoformat(self.current_cycle.start_time)
            end_dt = datetime.fromisoformat(self.current_cycle.end_time)
            self.current_cycle.cycle_duration_seconds = (end_dt - start_dt).total_seconds()
            
            # Save cycle data
            self._save_cycle_stats(self.current_cycle)
            status = 'completed' if self.current_cycle.total_errors == 0 else 'completed_with_errors'
            get_db_manager().save_cycle(
                cycle_id=self.current_cycle.cycle_id,
                start_time=self.current_cycle.start_time,
                end_time=self.current_cycle.end_time,
                websites_processed=self.current_cycle.websites_crawled,
                links_discovered=self.current_cycle.links_discovered,
                photos_downloaded=self.current_cycle.photos_downloaded,
                new_websites_added=self.current_cycle.new_websites_added,
                status=status,
            )

            logger.info(f"\n✅ CYCLE COMPLETE: {cycle_id}")
            logger.info(f"Duration: {self.current_cycle.cycle_duration_seconds:.1f} seconds")
            logger.info(f"Websites crawled: {self.current_cycle.websites_crawled}")
            logger.info(f"Websites scraped: {self.current_cycle.websites_scraped}")
            logger.info(f"Links discovered: {self.current_cycle.links_discovered}")
            logger.info(f"New websites added: {self.current_cycle.new_websites_added}")
            logger.info(f"Photos downloaded: {self.current_cycle.photos_downloaded}")
            
            return self.current_cycle

        except KeyboardInterrupt:
            logger.warning("INTERRUPT: Cycle interrupted by user (Ctrl+C). Saving partial progress.")
            if self.current_cycle:
                self.current_cycle.total_errors += 1
                self.current_cycle.end_time = datetime.now().isoformat()
                start_dt = datetime.fromisoformat(self.current_cycle.start_time)
                end_dt = datetime.fromisoformat(self.current_cycle.end_time)
                self.current_cycle.cycle_duration_seconds = (end_dt - start_dt).total_seconds()
                self._save_cycle_stats(self.current_cycle)
                get_db_manager().save_cycle(
                    cycle_id=self.current_cycle.cycle_id,
                    start_time=self.current_cycle.start_time,
                    end_time=self.current_cycle.end_time,
                    websites_processed=self.current_cycle.websites_crawled,
                    links_discovered=self.current_cycle.links_discovered,
                    photos_downloaded=self.current_cycle.photos_downloaded,
                    new_websites_added=self.current_cycle.new_websites_added,
                    status='interrupted',
                )
            raise

        except Exception as e:
            logger.error(f"ERROR: Cycle failed: {e}")
            if self.current_cycle:
                self.current_cycle.total_errors += 1
                self.current_cycle.end_time = datetime.now().isoformat()
                self._save_cycle_stats(self.current_cycle)
                get_db_manager().save_cycle(
                    cycle_id=self.current_cycle.cycle_id,
                    start_time=self.current_cycle.start_time,
                    end_time=self.current_cycle.end_time,
                    websites_processed=self.current_cycle.websites_crawled,
                    links_discovered=self.current_cycle.links_discovered,
                    photos_downloaded=self.current_cycle.photos_downloaded,
                    new_websites_added=self.current_cycle.new_websites_added,
                    status='failed',
                )
            raise
        finally:
            self.is_running = False
    
    def get_cycle_summary(self) -> Dict[str, Any]:
        """Get summary of recent cycles"""
        recent_cycles = self._get_recent_cycles(10)
        
        if not recent_cycles:
            return {'message': 'No cycles found'}
        
        total_websites_added = sum(c.new_websites_added for c in recent_cycles)
        total_photos = sum(c.photos_downloaded for c in recent_cycles)
        total_links = sum(c.links_discovered for c in recent_cycles)
        
        return {
            'recent_cycles': len(recent_cycles),
            'total_websites_discovered': total_websites_added,
            'total_photos_downloaded': total_photos,
            'total_links_discovered': total_links,
            'last_cycle': recent_cycles[0] if recent_cycles else None,
            'cycles': [asdict(c) for c in recent_cycles[:5]]  # Last 5 cycles
        }


# Convenience functions for main.py integration
async def run_automated_cycle(max_cycles: int = 1, 
                             concurrent_websites: int = 3,
                             discovery_enabled: bool = True,
                             scraping_enabled: bool = True) -> CycleStats:
    """Run automated discovery and scraping cycle"""
    
    if not MODULES_AVAILABLE:
        raise ImportError("Required modules not available. Please check dependencies.")
    
    # Always prompt for download location if scraping is enabled
    custom_download_dir = None
    if scraping_enabled:
        try:
            custom_download_dir = prompt_for_download_location("cycle_photos", "downloads")
        except Exception as e:
            logger.warning(f"Warning: Could not prompt for download location: {e}")
            custom_download_dir = "downloads"
            logger.info(f"Using default download location: {custom_download_dir}")
    
    # Load configuration from automation folder
    config = load_cycle_config()
    rate_config_data = config.get("rate_limiting", {})
    safety_limits = config.get("safety_limits", {})
    
    # Configure rate limiting from loaded config
    rate_config = RateLimitConfig(
        requests_per_minute=rate_config_data.get("requests_per_minute", 20),
        requests_per_hour=rate_config_data.get("requests_per_hour", 800),
        requests_per_day=rate_config_data.get("requests_per_day", 5000),
        min_delay_between_requests=rate_config_data.get("min_delay_between_requests", 3.0),
        max_delay_between_requests=rate_config_data.get("max_delay_between_requests", 10.0)
    )
    
    # Apply safety limits
    max_concurrent = min(
        concurrent_websites, 
        safety_limits.get("max_concurrent_websites", 10)
    )
    
    # Use configured chunk size
    cycle_settings = config.get("cycle_settings", {})
    chunk_size = cycle_settings.get("default_chunk_size", 5)
    
    manager = CycleManager(
        rate_limit_config=rate_config,
        max_concurrent_websites=max_concurrent,
        chunk_size=chunk_size,
        custom_download_dir=custom_download_dir
    )
    
    return await manager.run_cycle(max_cycles, discovery_enabled, scraping_enabled)


def get_automation_summary() -> Dict[str, Any]:
    """Get summary of automation cycles"""
    manager = CycleManager()
    return manager.get_cycle_summary()
