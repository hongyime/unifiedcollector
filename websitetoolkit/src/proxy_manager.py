"""
Unified Website Toolkit - Proxy Management System
Handles proxy rotation, testing, and management for web scraping
"""
import os
import random
import time
import json
import requests
from typing import List, Dict, Optional, Set, Tuple
from datetime import datetime, timedelta
import threading
from concurrent.futures import ThreadPoolExecutor


class ProxyManager:
    """Manages proxy rotation and validation for web scraping"""
    
    def __init__(self, proxy_file: str = None):
        self.proxies = []  # List of proxy dictionaries
        self.current_proxy_index = 0
        self.failed_proxies = set()  # Indices of failed proxies
        self.working_proxies = set()  # Indices of verified working proxies
        self.proxy_stats = {}  # Performance stats for each proxy
        self.last_test_time = {}  # Last test time for each proxy
        self.rotation_enabled = True
        self.proxy_file = proxy_file or "data/proxies.txt"
        
        # Performance tracking
        self.total_requests = 0
        self.successful_requests = 0
        self.proxy_switches = 0
        
        # Load proxies on initialization
        self._ensure_proxy_file_exists()
        self.load_proxies_from_file()
    
    def _ensure_proxy_file_exists(self):
        """Ensure proxy file exists with sample proxies"""
        os.makedirs(os.path.dirname(self.proxy_file), exist_ok=True)
        
        if not os.path.exists(self.proxy_file):
            self._create_sample_proxy_file()
    
    def _create_sample_proxy_file(self):
        """Create sample proxy file with free proxies"""
        sample_proxies = [
            "# Proxy list — add one proxy per line",
            "# Format: http://ip:port",
            "#         http://username:password@ip:port",
            "#         socks5://ip:port",
            "#         socks5://username:password@ip:port",
            "",
            "# Add your working proxies here:",
            ""
        ]
        
        try:
            with open(self.proxy_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sample_proxies))
            print(f"✅ Created sample proxy file: {self.proxy_file}")
            print("🔧 Edit this file to add your working proxies")
        except Exception as e:
            print(f"❌ Error creating proxy file: {e}")
    
    def add_proxy(self, proxy_url: str, proxy_type: str = 'http') -> bool:
        """Add a single proxy to the pool"""
        try:
            # Parse proxy URL to validate format
            if '://' not in proxy_url:
                # Assume HTTP if no protocol specified
                proxy_url = f"http://{proxy_url}"
            
            # Create proxy configuration
            if proxy_url.startswith('socks'):
                proxy_config = {
                    'http': proxy_url,
                    'https': proxy_url,
                    'type': 'socks',
                    'url': proxy_url
                }
            else:
                proxy_config = {
                    'http': proxy_url,
                    'https': proxy_url,
                    'type': 'http',
                    'url': proxy_url
                }
            
            # Check for duplicates
            for existing_proxy in self.proxies:
                if existing_proxy.get('url') == proxy_url:
                    print(f"⚠️ Proxy already exists: {proxy_url}")
                    return False
            
            self.proxies.append(proxy_config)
            proxy_index = len(self.proxies) - 1
            self.proxy_stats[proxy_index] = {
                'requests': 0,
                'successes': 0,
                'failures': 0,
                'avg_response_time': 0,
                'last_used': None,
                'added_time': datetime.now().isoformat()
            }
            
            print(f"✅ Added proxy: {proxy_url}")
            return True
            
        except Exception as e:
            print(f"❌ Error adding proxy {proxy_url}: {e}")
            return False
    
    def add_proxy_list(self, proxy_list: List[str]) -> int:
        """Add multiple proxies from a list"""
        added_count = 0
        for proxy in proxy_list:
            if proxy.strip() and self.add_proxy(proxy.strip()):
                added_count += 1
        return added_count
    
    def load_proxies_from_file(self, file_path: str = None) -> int:
        """Load proxies from a text file"""
        file_path = file_path or self.proxy_file
        
        if not os.path.exists(file_path):
            print(f"⚠️ Proxy file not found: {file_path}")
            return 0
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            proxies = []
            for line in lines:
                line = line.strip()
                # Skip empty lines and comments
                if line and not line.startswith('#'):
                    proxies.append(line)
            
            added_count = self.add_proxy_list(proxies)
            print(f"📁 Loaded {added_count} proxies from {file_path}")
            return added_count
            
        except Exception as e:
            print(f"❌ Error loading proxies from {file_path}: {e}")
            return 0
    
    def save_proxies_to_file(self, file_path: str = None) -> bool:
        """Save current proxy list to file"""
        file_path = file_path or self.proxy_file
        
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("# Proxy List - Auto-generated\n")
                f.write(f"# Generated: {datetime.now().isoformat()}\n")
                f.write(f"# Total proxies: {len(self.proxies)}\n\n")
                
                for i, proxy in enumerate(self.proxies):
                    url = proxy.get('url', 'Unknown')
                    stats = self.proxy_stats.get(i, {})
                    success_rate = 0
                    if stats.get('requests', 0) > 0:
                        success_rate = (stats.get('successes', 0) / stats.get('requests', 1)) * 100
                    
                    f.write(f"# Proxy {i+1}: Success rate: {success_rate:.1f}%\n")
                    f.write(f"{url}\n\n")
            
            print(f"💾 Saved {len(self.proxies)} proxies to {file_path}")
            return True
            
        except Exception as e:
            print(f"❌ Error saving proxies to {file_path}: {e}")
            return False
    
    def get_next_proxy(self) -> Optional[Dict]:
        """Get the next working proxy in rotation"""
        if not self.proxies:
            return None
        
        if not self.rotation_enabled:
            # Return the first working proxy if rotation is disabled
            for i, proxy in enumerate(self.proxies):
                if i not in self.failed_proxies:
                    return proxy
            return None
        
        # Get available proxies (not failed or recently failed)
        available_proxies = []
        for i, proxy in enumerate(self.proxies):
            if i not in self.failed_proxies:
                available_proxies.append((i, proxy))
        
        if not available_proxies:
            # Reset failed proxies if all are marked as failed
            print("🔄 All proxies failed, resetting failure list...")
            self.failed_proxies.clear()
            available_proxies = [(i, proxy) for i, proxy in enumerate(self.proxies)]
        
        if available_proxies:
            # Choose next proxy in rotation
            proxy_index, proxy = available_proxies[self.current_proxy_index % len(available_proxies)]
            self.current_proxy_index += 1
            self.proxy_switches += 1
            
            # Update usage stats
            if proxy_index in self.proxy_stats:
                self.proxy_stats[proxy_index]['last_used'] = datetime.now().isoformat()
            
            return proxy
        
        return None
    
    def get_random_proxy(self) -> Optional[Dict]:
        """Get a random working proxy"""
        if not self.proxies:
            return None
        
        available_proxies = [
            (i, proxy) for i, proxy in enumerate(self.proxies) 
            if i not in self.failed_proxies
        ]
        
        if not available_proxies:
            self.failed_proxies.clear()
            available_proxies = [(i, proxy) for i, proxy in enumerate(self.proxies)]
        
        if available_proxies:
            proxy_index, proxy = random.choice(available_proxies)
            self.proxy_switches += 1
            
            if proxy_index in self.proxy_stats:
                self.proxy_stats[proxy_index]['last_used'] = datetime.now().isoformat()
            
            return proxy
        
        return None
    
    def mark_proxy_failed(self, proxy: Dict, permanent: bool = False):
        """Mark a proxy as failed"""
        try:
            proxy_url = proxy.get('url', proxy.get('http', 'Unknown'))
            
            # Find proxy index
            proxy_index = None
            for i, p in enumerate(self.proxies):
                if p.get('url') == proxy_url or p.get('http') == proxy_url:
                    proxy_index = i
                    break
            
            if proxy_index is not None:
                if not permanent:
                    # Temporary failure - will retry later
                    self.failed_proxies.add(proxy_index)
                    print(f"⚠️ Marked proxy as temporarily failed: {proxy_url}")
                else:
                    # Permanent failure - remove from list
                    self.proxies.pop(proxy_index)
                    # Update indices in failed_proxies and working_proxies
                    self.failed_proxies = {i-1 if i > proxy_index else i for i in self.failed_proxies if i != proxy_index}
                    self.working_proxies = {i-1 if i > proxy_index else i for i in self.working_proxies if i != proxy_index}
                    if proxy_index in self.proxy_stats:
                        del self.proxy_stats[proxy_index]
                    print(f"❌ Permanently removed failed proxy: {proxy_url}")
                
                # Update stats
                if proxy_index in self.proxy_stats:
                    self.proxy_stats[proxy_index]['failures'] += 1
                    
        except Exception as e:
            print(f"❌ Error marking proxy as failed: {e}")
    
    def mark_proxy_working(self, proxy: Dict, response_time: float = 0):
        """Mark a proxy as working"""
        try:
            proxy_url = proxy.get('url', proxy.get('http', 'Unknown'))
            
            # Find proxy index
            proxy_index = None
            for i, p in enumerate(self.proxies):
                if p.get('url') == proxy_url or p.get('http') == proxy_url:
                    proxy_index = i
                    break
            
            if proxy_index is not None:
                # Remove from failed list
                self.failed_proxies.discard(proxy_index)
                self.working_proxies.add(proxy_index)
                
                # Update stats
                if proxy_index in self.proxy_stats:
                    stats = self.proxy_stats[proxy_index]
                    stats['successes'] += 1
                    stats['requests'] += 1
                    
                    # Update average response time
                    if response_time > 0:
                        current_avg = stats.get('avg_response_time', 0)
                        success_count = stats.get('successes', 1)
                        stats['avg_response_time'] = ((current_avg * (success_count - 1)) + response_time) / success_count
                
                print(f"✅ Confirmed working proxy: {proxy_url}")
                
        except Exception as e:
            print(f"❌ Error marking proxy as working: {e}")
    
    def test_proxy(self, proxy: Dict, timeout: int = 10) -> Tuple[bool, float]:
        """Test if a proxy is working"""
        test_urls = [
            "https://httpbin.org/ip",
            "https://api.ipify.org?format=json",
            "https://icanhazip.com"
        ]
        
        proxy_url = proxy.get('url', proxy.get('http', 'Unknown'))
        
        for test_url in test_urls:
            try:
                start_time = time.time()
                
                response = requests.get(
                    test_url,
                    proxies=proxy,
                    timeout=timeout,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                )
                
                response_time = time.time() - start_time
                
                if response.status_code == 200:
                    # Try to parse response to verify it's working
                    try:
                        if 'json' in response.headers.get('content-type', ''):
                            response.json()
                        else:
                            # For plain text responses, just check they contain an IP
                            import re
                            ip_pattern = r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
                            if re.search(ip_pattern, response.text):
                                pass  # Valid IP response
                    except:
                        pass  # Response parsing failed, but status was 200
                    
                    print(f"✅ Proxy test successful: {proxy_url} ({response_time:.2f}s)")
                    return True, response_time
                    
            except Exception as e:
                print(f"⚠️ Proxy test failed for {proxy_url}: {e}")
                continue
        
        print(f"❌ Proxy test failed: {proxy_url}")
        return False, 0.0
    
    def test_all_proxies(self, timeout: int = 10, max_workers: int = 5) -> Dict[str, int]:
        """Test all proxies concurrently"""
        if not self.proxies:
            print("⚠️ No proxies to test")
            return {'working': 0, 'failed': 0, 'total': 0}
        
        print(f"🧪 Testing {len(self.proxies)} proxies...")
        
        working_count = 0
        failed_count = 0
        
        def test_single_proxy(proxy_data):
            index, proxy = proxy_data
            is_working, response_time = self.test_proxy(proxy, timeout)
            if is_working:
                self.mark_proxy_working(proxy, response_time)
                return 'working'
            else:
                self.mark_proxy_failed(proxy)
                return 'failed'
        
        # Test proxies concurrently
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(test_single_proxy, enumerate(self.proxies)))
        
        working_count = results.count('working')
        failed_count = results.count('failed')
        
        print(f"📊 Proxy test results: {working_count} working, {failed_count} failed")
        
        return {
            'working': working_count,
            'failed': failed_count,
            'total': len(self.proxies)
        }
    
    def get_proxy_stats(self) -> Dict:
        """Get comprehensive proxy statistics"""
        total_proxies = len(self.proxies)
        working_proxies = len(self.working_proxies)
        failed_proxies = len(self.failed_proxies)
        
        # Calculate overall success rate
        total_requests = sum(stats.get('requests', 0) for stats in self.proxy_stats.values())
        total_successes = sum(stats.get('successes', 0) for stats in self.proxy_stats.values())
        overall_success_rate = (total_successes / total_requests * 100) if total_requests > 0 else 0
        
        # Find best performing proxy
        best_proxy = None
        best_success_rate = 0
        for i, stats in self.proxy_stats.items():
            if i < len(self.proxies) and stats.get('requests', 0) > 0:
                success_rate = stats.get('successes', 0) / stats.get('requests', 1) * 100
                if success_rate > best_success_rate:
                    best_success_rate = success_rate
                    best_proxy = self.proxies[i].get('url', 'Unknown')
        
        return {
            'total_proxies': total_proxies,
            'working_proxies': working_proxies,
            'failed_proxies': failed_proxies,
            'proxy_switches': self.proxy_switches,
            'total_requests': self.total_requests,
            'successful_requests': self.successful_requests,
            'overall_success_rate': overall_success_rate,
            'best_proxy': best_proxy,
            'best_success_rate': best_success_rate
        }
    
    def display_proxy_stats(self):
        """Display formatted proxy statistics"""
        stats = self.get_proxy_stats()
        
        print("\n📊 PROXY MANAGER STATISTICS")
        print("=" * 50)
        print(f"Total Proxies: {stats['total_proxies']}")
        print(f"Working Proxies: {stats['working_proxies']}")
        print(f"Failed Proxies: {stats['failed_proxies']}")
        print(f"Proxy Switches: {stats['proxy_switches']}")
        print(f"Total Requests: {stats['total_requests']}")
        print(f"Success Rate: {stats['overall_success_rate']:.1f}%")
        
        if stats['best_proxy']:
            print(f"Best Proxy: {stats['best_proxy']} ({stats['best_success_rate']:.1f}%)")
        
        print("\n📋 Individual Proxy Performance:")
        print("-" * 50)
        
        for i, proxy in enumerate(self.proxies):
            if i in self.proxy_stats:
                stats_data = self.proxy_stats[i]
                proxy_url = proxy.get('url', 'Unknown')
                requests = stats_data.get('requests', 0)
                successes = stats_data.get('successes', 0)
                success_rate = (successes / requests * 100) if requests > 0 else 0
                avg_time = stats_data.get('avg_response_time', 0)
                
                status = "✅" if i in self.working_proxies else "❌" if i in self.failed_proxies else "❓"
                
                print(f"{status} {proxy_url}")
                print(f"   Requests: {requests}, Success Rate: {success_rate:.1f}%, Avg Time: {avg_time:.2f}s")
        
        print("=" * 50)
    
    def reset_failed_proxies(self):
        """Reset all failed proxies for retry"""
        self.failed_proxies.clear()
        print("🔄 Reset all failed proxies - they will be retried")
    
    def enable_rotation(self, enabled: bool = True):
        """Enable or disable proxy rotation"""
        self.rotation_enabled = enabled
        status = "enabled" if enabled else "disabled"
        print(f"🔄 Proxy rotation {status}")
    
    def clear_all_proxies(self):
        """Remove all proxies"""
        self.proxies.clear()
        self.failed_proxies.clear()
        self.working_proxies.clear()
        self.proxy_stats.clear()
        print("🗑️ Cleared all proxies")


class UserAgentRotator:
    """Rotates user agents to avoid detection"""
    
    def __init__(self):
        self.user_agents = [
            # Chrome Windows
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
            
            # Chrome macOS
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            
            # Firefox
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
            
            # Safari
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
            
            # Edge
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0',
            
            # Chrome Linux
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        ]
        self.current_index = 0
    
    def get_random_user_agent(self) -> str:
        """Get a random user agent"""
        return random.choice(self.user_agents)
    
    def get_next_user_agent(self) -> str:
        """Get the next user agent in rotation"""
        ua = self.user_agents[self.current_index % len(self.user_agents)]
        self.current_index += 1
        return ua
    
    def add_user_agent(self, user_agent: str):
        """Add a custom user agent to the list"""
        if user_agent not in self.user_agents:
            self.user_agents.append(user_agent)
            print(f"✅ Added custom user agent")


# Utility functions
def test_proxy_manager():
    """Test the proxy manager functionality"""
    print("🧪 Testing Proxy Manager")
    print("=" * 40)
    
    # Create proxy manager
    pm = ProxyManager()
    
    # Display current stats
    pm.display_proxy_stats()
    
    # Test all proxies
    if pm.proxies:
        print("\n🔄 Testing all proxies...")
        test_results = pm.test_all_proxies(timeout=10, max_workers=3)
        print(f"📊 Test completed: {test_results}")
        
        # Display updated stats
        pm.display_proxy_stats()
    else:
        print("⚠️ No proxies loaded. Add proxies to the proxy file first.")
    
    return pm


if __name__ == "__main__":
    test_proxy_manager()
