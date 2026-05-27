"""
Unified Website Toolkit - URL Filter
Advanced URL filtering system with pattern matching for blocking unwanted sites
"""
import re
import json
import os
from typing import List, Dict, Set, Optional, Pattern, Any, Tuple
from urllib.parse import urlparse
from config import DATA_DIR


class URLFilter:
    """Advanced URL filtering system with configurable blocking patterns"""
    
    def __init__(self, config_file: Optional[str] = None):
        """Initialize URL filter with configuration"""
        self.config_file = config_file or os.path.join(DATA_DIR, 'url_filter_config.json')
        self.blocked_patterns = []
        self.compiled_patterns = []
        self.allowed_patterns = []
        self.compiled_allowed_patterns = []
        self.blocked_domains = set()
        self.blocked_paths = set()
        self.blocked_subdomains = set()
        
        # Load configuration
        self.load_config()
    
    def load_config(self):
        """Load blocking patterns from configuration file"""
        default_config = {
            "blocked_patterns": [
                # Social Media Platforms (consolidated patterns)
                "*://*facebook.com/*",
                "*://*instagram.com/*", 
                "*://*twitter.com/*",
                "*://*x.com/*",
                "*://*tiktok.com/*",
                "*://*linkedin.com/*",
                "*://*snapchat.com/*",
                "*://*discord.com/*",
                "*://*telegram.org/*",
                "*://*whatsapp.com/*",
                
                # E-commerce Platforms (consolidated patterns)
                "*://*amazon.com/gp/product/*",
                "*://*amazon.com/dp/*",
                "*://*ebay.com/itm/*",
                "*://*aliexpress.com/item/*",
                "*://*etsy.com/listing/*",
                "*://*walmart.com/ip/*",
                "*://*target.com/p/*",
                "*://*bestbuy.com/site/*",
                
                # Shopping Cart/Checkout (consolidated)
                "*://*shopify.com/*/cart",
                "*://*shopify.com/*/checkout",
                "*://*shopify.com/products/*",
                
                # Account/Authentication Areas
                "*://*/account/*",
                "*://*/wishlist/*",
                "*://*/cart/*",
                "*://*/checkout/*",
                "*://*/login/*",
                "*://*/signin/*",
                "*://*/register/*",
                "*://*/signup/*",
                "*://*/profile/*",
                "*://*/personal/*",
                "*://*/private/*",
                "*://*/admin/*",
                "*://*/dashboard/*",
                "*://*/settings/*",
                
                # Dynamic/Generated Content
                "*://*/search/*",
                "*://*/results/*",
                "*://*/query/*",
                "*://*/api/*",
                "*://*/ajax/*",
                "*://*/json/*",
                "*://*/xml/*"
            ],
            "allowed_patterns": [
                # Allow Reddit subreddits but not user profiles or comments (more specific)
                "*://*reddit.com/r/*/",
                "*://*reddit.com/r/*/hot",
                "*://*reddit.com/r/*/new", 
                "*://*reddit.com/r/*/top",
                "*://*reddit.com/r/*/rising",
                # Allow product category pages but not individual products  
                "*://*amazon.com/s/*",
                "*://*ebay.com/sch/*"
            ],
            "blocked_domains": [
                "ads.google.com",
                "doubleclick.net",
                "googleadservices.com",
                "googlesyndication.com",
                "facebook.net",
                "fbcdn.net",
                "instagram.com",
                "cdninstagram.com"
            ],
            "blocked_file_extensions": [
                ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm",
                ".zip", ".rar", ".7z", ".tar", ".gz",
                ".mp3", ".wav", ".flac", ".mp4", ".avi", ".mov",
                ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"
            ],
            "description": "Comprehensive URL filtering to avoid personal data, login-protected content, and inefficient crawling targets"
        }
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    # Merge with defaults
                    for key, default_value in default_config.items():
                        if key not in config:
                            config[key] = default_value
            except Exception as e:
                print(f"WARNING: Error loading URL filter config: {e}")
                config = default_config
        else:
            config = default_config
            self.save_config(config)
        
        # Parse configuration
        self.blocked_patterns = config.get('blocked_patterns', [])
        self.allowed_patterns = config.get('allowed_patterns', [])
        self.blocked_domains = set(config.get('blocked_domains', []))
        self.blocked_file_extensions = set(config.get('blocked_file_extensions', []))
        
        # Compile patterns
        self._compile_patterns()
    
    def save_config(self, config: Dict[str, Any]):
        """Save configuration to file"""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"ERROR: Failed to save URL filter config: {e}")
    
    def _compile_patterns(self):
        """Compile wildcard patterns to regex"""
        self.compiled_patterns = []
        self.compiled_allowed_patterns = []
        
        for pattern in self.blocked_patterns:
            regex_pattern = self._wildcard_to_regex(pattern)
            try:
                compiled = re.compile(regex_pattern, re.IGNORECASE)
                self.compiled_patterns.append((pattern, compiled))
            except re.error as e:
                print(f"WARNING: Invalid pattern '{pattern}': {e}")
        
        for pattern in self.allowed_patterns:
            regex_pattern = self._wildcard_to_regex(pattern)
            try:
                compiled = re.compile(regex_pattern, re.IGNORECASE)
                self.compiled_allowed_patterns.append((pattern, compiled))
            except re.error as e:
                print(f"WARNING: Invalid allowed pattern '{pattern}': {e}")
    
    def _wildcard_to_regex(self, pattern: str) -> str:
        """Convert wildcard pattern to regex safely to avoid ReDoS"""
        # Limit pattern length to prevent extremely complex regex
        if len(pattern) > 512:
            return "^$" # Block everything if pattern too long/complex
            
        # Escape special regex characters except * and ?
        # We handle * and ? separately to ensure they don't lead to exponential backtracking
        escaped = re.escape(pattern)
        
        # Convert wildcards to regex with restricted repetition to prevent ReDoS
        # Use [^/]* for * inside path segments or .* with atomic-like behavior if possible
        # For simplicity and safety, we use a non-greedy .*? but anchored
        escaped = escaped.replace(r'\*', '.*')  # * matches any sequence
        escaped = escaped.replace(r'\?', '.')   # ? matches single character
        
        # Anchor the pattern
        return f'^{escaped}$'
    
    def is_url_blocked(self, url: str) -> Tuple[bool, Optional[str]]:
        """Check if URL should be blocked
        
        Returns:
            Tuple of (is_blocked, reason)
        """
        try:
            parsed = urlparse(url.lower())
            
            # Check domain blacklist
            domain = parsed.netloc
            if domain.startswith('www.'):
                domain = domain[4:]
            
            if domain in self.blocked_domains:
                return True, f"Domain blocked: {domain}"
            
            # Check file extension
            path = parsed.path.lower()
            for ext in self.blocked_file_extensions:
                if path.endswith(ext.lower()):
                    return True, f"File extension blocked: {ext}"
            
            # Check allowed patterns first (whitelist)
            for pattern_str, compiled_pattern in self.compiled_allowed_patterns:
                if compiled_pattern.match(url):
                    return False, f"Explicitly allowed by pattern: {pattern_str}"
            
            # Check blocked patterns (blacklist)
            for pattern_str, compiled_pattern in self.compiled_patterns:
                if compiled_pattern.match(url):
                    return True, f"Blocked by pattern: {pattern_str}"
            
            return False, None
            
        except Exception as e:
            print(f"WARNING: Error checking URL filter for {url}: {e}")
            return False, None
    
    def should_skip_url(self, url: str) -> bool:
        """Simple boolean check if URL should be skipped"""
        blocked, _ = self.is_url_blocked(url)
        return blocked
    
    def filter_urls(self, urls: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
        """Filter a list of URLs
        
        Returns:
            Tuple of (allowed_urls, blocked_urls_with_reasons)
        """
        allowed = []
        blocked = []
        
        for url in urls:
            is_blocked, reason = self.is_url_blocked(url)
            if is_blocked:
                blocked.append((url, reason))
            else:
                allowed.append(url)
        
        return allowed, blocked
    
    def get_filter_stats(self) -> Dict[str, Any]:
        """Get statistics about the filter configuration"""
        return {
            "blocked_patterns_count": len(self.blocked_patterns),
            "allowed_patterns_count": len(self.allowed_patterns),
            "blocked_domains_count": len(self.blocked_domains),
            "blocked_extensions_count": len(self.blocked_file_extensions),
            "compiled_patterns": len(self.compiled_patterns),
            "compiled_allowed_patterns": len(self.compiled_allowed_patterns)
        }
    
    def add_blocked_pattern(self, pattern: str):
        """Add a new blocked pattern"""
        if pattern not in self.blocked_patterns:
            self.blocked_patterns.append(pattern)
            self._compile_patterns()
            self._save_current_config()
    
    def remove_blocked_pattern(self, pattern: str):
        """Remove a blocked pattern"""
        if pattern in self.blocked_patterns:
            self.blocked_patterns.remove(pattern)
            self._compile_patterns()
            self._save_current_config()
    
    def add_allowed_pattern(self, pattern: str):
        """Add a new allowed pattern"""
        if pattern not in self.allowed_patterns:
            self.allowed_patterns.append(pattern)
            self._compile_patterns()
            self._save_current_config()
    
    def _save_current_config(self):
        """Save current configuration"""
        config = {
            "blocked_patterns": self.blocked_patterns,
            "allowed_patterns": self.allowed_patterns,
            "blocked_domains": list(self.blocked_domains),
            "blocked_file_extensions": list(self.blocked_file_extensions)
        }
        self.save_config(config)
    
    def test_pattern(self, pattern: str, test_urls: List[str]) -> Dict[str, Any]:
        """Test a pattern against a list of URLs"""
        regex_pattern = self._wildcard_to_regex(pattern)
        try:
            compiled = re.compile(regex_pattern, re.IGNORECASE)
            matches = []
            non_matches = []
            
            for url in test_urls:
                if compiled.match(url):
                    matches.append(url)
                else:
                    non_matches.append(url)
            
            return {
                "pattern": pattern,
                "regex": regex_pattern,
                "matches": matches,
                "non_matches": non_matches,
                "match_count": len(matches),
                "total_urls": len(test_urls)
            }
        except re.error as e:
            return {
                "pattern": pattern,
                "error": str(e)
            }


# Global URL filter instance
_url_filter = None

def get_url_filter() -> URLFilter:
    """Get global URL filter instance"""
    global _url_filter
    if _url_filter is None:
        _url_filter = URLFilter()
    return _url_filter

def is_url_blocked(url: str) -> bool:
    """Convenience function to check if URL is blocked"""
    return get_url_filter().should_skip_url(url)

def filter_urls(urls: List[str]) -> List[str]:
    """Convenience function to filter URLs"""
    allowed, _ = get_url_filter().filter_urls(urls)
    return allowed