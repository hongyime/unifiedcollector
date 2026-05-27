"""
Search engine providers for Website Toolkit.

Provides search functionality using DuckDuckGo, Bing, and Serper.dev APIs.
"""

import json
import random
import time
from typing import Set, List, Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# Global instances (set by main.py)
_search_cache = None
_rate_limiter = None


def set_search_cache(cache):
    """Set the global search cache instance."""
    global _search_cache
    _search_cache = cache


def set_rate_limiter(limiter):
    """Set the global rate limiter instance."""
    global _rate_limiter
    _rate_limiter = limiter


def search_duckduckgo(query: str, max_results: int = 50) -> Set[str]:
    """Search using DuckDuckGo — no API key needed."""
    # Check cache first
    if _search_cache:
        cached = _search_cache.get(query, "duckduckgo")
        if cached:
            print(f"[DDG] Cache hit! Returning {len(cached)} cached results")
            return cached
    
    try:
        import warnings
        warnings.filterwarnings("ignore", message=".*Impersonate.*")
        warnings.filterwarnings("ignore", message=".*renamed.*")

        from duckduckgo_search import DDGS

        print(f"[DDG] Searching: {query}")
        results = set()
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                # Handle both old ('link') and new ('href') API formats
                url = r.get('href') or r.get('link') or r.get('url', '')
                if url:
                    results.add(url)
                # Rate limiting
                if _rate_limiter:
                    _rate_limiter.wait("https://duckduckgo.com")
                time.sleep(0.1)
        print(f"[DDG] Found {len(results)} results")
        
        # Cache results
        if _search_cache and results:
            _search_cache.set(query, "duckduckgo", results)
        
        return results
    except Exception as e:
        print(f"[DDG] Error: {e}")
        if _rate_limiter:
            _rate_limiter.record_failure("https://duckduckgo.com", 500)
        return set()


def search_bing(query: str, num_pages: int = 5) -> Set[str]:
    """Search using Bing web search with proper URL encoding."""
    # Check cache first
    if _search_cache:
        cached = _search_cache.get(query, "bing")
        if cached:
            print(f"[Bing] Cache hit! Returning {len(cached)} cached results")
            return cached
    
    try:
        print(f"[Bing] Searching: {query}")
        results = set()
        encoded_query = quote_plus(query)
        
        for page in range(0, num_pages * 10, 10):
            url = f'https://www.bing.com/search?q={encoded_query}&first={page}'
            
            # Rate limiting
            if _rate_limiter:
                _rate_limiter.wait("https://www.bing.com")
            
            try:
                response = requests.get(url, timeout=10, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                
                if response.status_code == 200:
                    if _rate_limiter:
                        _rate_limiter.record_success("https://www.bing.com")
                    soup = BeautifulSoup(response.text, 'html.parser')
                    for result in soup.select('li.b_algo h2 a'):
                        link = result.get('href')
                        if link and link.startswith('http'):
                            results.add(link)
                else:
                    if _rate_limiter:
                        _rate_limiter.record_failure("https://www.bing.com", response.status_code)
            except Exception as e:
                print(f"[Bing] Page {page} error: {e}")
                if _rate_limiter:
                    _rate_limiter.record_failure("https://www.bing.com", 500)
            
            time.sleep(random.uniform(1, 2.5))
        
        print(f"[Bing] Found {len(results)} results")
        
        # Cache results
        if _search_cache and results:
            _search_cache.set(query, "bing", results)
        
        return results
    except Exception as e:
        print(f"[Bing] Error: {e}")
        if _rate_limiter:
            _rate_limiter.record_failure("https://www.bing.com", 500)
        return set()


def search_serper(query: str, api_key: str) -> Set[str]:
    """Search using Serper.dev API (Google results) with regional params."""
    # Check cache first
    if _search_cache:
        cached = _search_cache.get(query, "serper")
        if cached:
            print(f"[Serper] Cache hit! Returning {len(cached)} cached results")
            return cached
    
    if not api_key:
        return set()
    
    try:
        print(f"[Serper API] Searching: {query}")
        results = set()
        url = "https://google.serper.dev/search"
        headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}
        
        # Rate limiting
        if _rate_limiter:
            _rate_limiter.wait("https://google.serper.dev")
        
        for page in range(3):
            payload = json.dumps({
                "q": query,
                "page": page + 1,
                "num": 100,
                "gl": "sg",
                "hl": "en",
            })
            
            response = requests.post(url, headers=headers, data=payload, timeout=15)
            
            if response.status_code == 401:
                print(f"[Serper] API key invalid or unauthorized.")
                return set()
            if response.status_code == 429:
                print(f"[Serper] API quota exceeded.")
                return set()
            if response.status_code == 200:
                if _rate_limiter:
                    _rate_limiter.record_success("https://google.serper.dev")
                data = response.json()
                if 'organic' in data:
                    for item in data['organic']:
                        if 'link' in item:
                            results.add(item['link'])
                else:
                    break
            else:
                if _rate_limiter:
                    _rate_limiter.record_failure("https://google.serper.dev", response.status_code)
                break
        
        print(f"[Serper API] Found {len(results)} results")
        
        # Cache results
        if _search_cache and results:
            _search_cache.set(query, "serper", results)
        
        return results
    except Exception as e:
        print(f"[Serper API] Error: {e}")
        if _rate_limiter:
            _rate_limiter.record_failure("https://google.serper.dev", 500)
        return set()


def get_search_results(query: str, max_total: int = 50, serper_api_key: Optional[str] = None) -> List[str]:
    """
    Waterfall search: DDG → Bing → Serper (if needed).
    
    Args:
        query: Search query string.
        max_total: Maximum number of results to return.
        serper_api_key: Optional Serper.dev API key.
    
    Returns:
        List of URLs (up to max_total).
    """
    all_results = set()

    # 1. DuckDuckGo (free, unlimited)
    ddg_res = search_duckduckgo(query, max_results=30)
    all_results.update(ddg_res)
    if len(all_results) >= max_total:
        return list(all_results)[:max_total]

    time.sleep(2)

    # 2. Bing (free, scraping)
    bing_res = search_bing(query, num_pages=3)
    all_results.update(bing_res)
    if len(all_results) >= max_total:
        return list(all_results)[:max_total]

    # 3. Serper — ONLY if DDG + Bing found almost nothing (< 5 results)
    #    Conserves limited API credits
    if len(all_results) < 5 and serper_api_key:
        time.sleep(2)
        print(f"[Info] DDG + Bing found < 5 results, using Serper API credits...")
        serper_res = search_serper(query, serper_api_key)
        all_results.update(serper_res)
        if len(all_results) >= max_total:
            return list(all_results)[:max_total]

    return list(all_results)[:max_total]
