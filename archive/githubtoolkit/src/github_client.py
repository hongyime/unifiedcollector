"""GitHub API client with rate limiting and authentication."""
import time
import asyncio
from typing import Optional, Dict, List, Any
from datetime import datetime
import aiohttp

from src.config import Config
from src.pat_manager import PATManager


class GitHubAPIClient:
    """GitHub REST API v3 client with rate limiting and multi-PAT rotation."""

    def __init__(self, pat=None, pats: Optional[List[str]] = None):
        """Initialize GitHub API client.

        Args:
            pat: Single PAT (legacy). If pats list also given, pat is prepended.
            pats: List of PATs for rotation. Rotates to next token when rate limit low.
        """
        self.base_url = Config.GITHUB_API_BASE
        # Build token pool
        all_pats = list(pats) if pats else []
        if pat and pat not in all_pats:
            all_pats.insert(0, pat)
        self._pats: List[str] = all_pats
        self._pat_index: int = 0
        self.pat: Optional[str] = all_pats[0] if all_pats else None

        self.session: Optional[aiohttp.ClientSession] = None
        self.rate_limit_remaining: Optional[int] = None
        self.rate_limit_reset: Optional[int] = None
        self.rate_limit_limit: Optional[int] = None
        self.requests_made = 0
        self.requests_cached = 0

    def _current_pat(self) -> Optional[str]:
        return self._pats[self._pat_index] if self._pats else None

    def _rotate_pat(self):
        """Rotate to next PAT in pool."""
        if len(self._pats) > 1:
            self._pat_index = (self._pat_index + 1) % len(self._pats)
            self.pat = self._pats[self._pat_index]
            self.rate_limit_remaining = None
            print(f"🔄 Rotated to PAT {self._pat_index + 1}/{len(self._pats)}")
            # Recreate session with new token
            # (handled lazily on next request via _ensure_session)

    async def __aenter__(self):
        headers = {
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'GitHub-Toolkit/2.0'
        }
        pat = self._current_pat()
        if pat:
            headers['Authorization'] = f'token {pat}'
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()
        return False
    
    def _update_rate_limit(self, headers: Dict[str, str]):
        """Update rate limit state from response headers."""
        if 'X-RateLimit-Remaining' in headers:
            self.rate_limit_remaining = int(headers['X-RateLimit-Remaining'])
        if 'X-RateLimit-Reset' in headers:
            self.rate_limit_reset = int(headers['X-RateLimit-Reset'])
        if 'X-RateLimit-Limit' in headers:
            self.rate_limit_limit = int(headers['X-RateLimit-Limit'])
    
    async def _check_rate_limit(self):
        """Check rate limit; rotate PAT if low, wait if all exhausted."""
        if self.rate_limit_remaining is None:
            return
        if self.rate_limit_remaining < Config.API_RATE_LIMIT_BUFFER:
            # Try rotating to another PAT first
            if len(self._pats) > 1:
                self._rotate_pat()
                # Rebuild session auth header
                if self.session:
                    await self.session.close()
                pat = self._current_pat()
                headers = {'Accept': 'application/vnd.github.v3+json',
                           'User-Agent': 'GitHub-Toolkit/2.0'}
                if pat:
                    headers['Authorization'] = f'token {pat}'
                self.session = aiohttp.ClientSession(
                    headers=headers, timeout=aiohttp.ClientTimeout(total=30))
                self.rate_limit_remaining = None
                return
            # Single PAT — wait for reset
            if self.rate_limit_reset:
                wait_time = self.rate_limit_reset - int(time.time())
                if wait_time > 0:
                    print(f"⏳ Rate limit low ({self.rate_limit_remaining}). Waiting {wait_time}s...")
                    await asyncio.sleep(wait_time + 1)
    
    async def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Make API request with rate limiting.
        
        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (e.g., '/users/octocat')
            **kwargs: Additional arguments for aiohttp request
            
        Returns:
            JSON response as dict or None on error
        """
        await self._check_rate_limit()
        
        url = f"{self.base_url}{endpoint}"
        
        try:
            async with self.session.request(method, url, **kwargs) as response:
                self._update_rate_limit(response.headers)
                self.requests_made += 1
                
                if response.status == 200:
                    return await response.json()
                elif response.status == 204:
                    return {}
                elif response.status == 404:
                    return None
                elif response.status == 403:
                    # Rate limit exceeded
                    print(f"❌ Rate limit exceeded (403)")
                    await self._check_rate_limit()
                    return None
                elif response.status == 401:
                    print(f"❌ Authentication failed (401). Check your PAT token.")
                    return None
                else:
                    print(f"❌ API error {response.status}: {endpoint}")
                    return None
        
        except asyncio.TimeoutError:
            print(f"⏱️ Timeout: {endpoint}")
            return None
        except aiohttp.ClientError as e:
            print(f"❌ Network error: {e}")
            return None
    
    async def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user profile data.
        
        Args:
            username: GitHub username
            
        Returns:
            User data dict or None if not found
        """
        return await self._request('GET', f'/users/{username}')
    
    async def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user profile data by numeric ID.
        
        Args:
            user_id: GitHub user ID
            
        Returns:
            User data dict or None if not found
        """
        return await self._request('GET', f'/user/{user_id}')
    
    async def get_followers(self, username: str, page: int = 1, per_page: int = 100) -> List[Dict[str, Any]]:
        """Get user's followers.
        
        Args:
            username: GitHub username
            page: Page number (1-indexed)
            per_page: Results per page (max 100)
            
        Returns:
            List of follower user dicts
        """
        result = await self._request('GET', f'/users/{username}/followers', params={'page': page, 'per_page': per_page})
        return result if result else []
    
    async def get_following(self, username: str, page: int = 1, per_page: int = 100) -> List[Dict[str, Any]]:
        """Get users that this user follows.
        
        Args:
            username: GitHub username
            page: Page number (1-indexed)
            per_page: Results per page (max 100)
            
        Returns:
            List of following user dicts
        """
        result = await self._request('GET', f'/users/{username}/following', params={'page': page, 'per_page': per_page})
        return result if result else []
    
    async def get_all_followers(self, username: str) -> List[Dict[str, Any]]:
        """Get all followers for a user (paginated).
        
        Args:
            username: GitHub username
            
        Returns:
            List of all follower user dicts
        """
        all_followers = []
        page = 1
        
        while True:
            followers = await self.get_followers(username, page=page, per_page=100)
            if not followers:
                break
            all_followers.extend(followers)
            page += 1
            
            # Rate limiting delay
            await asyncio.sleep(Config.API_REQUEST_DELAY)
        
        return all_followers
    
    async def get_all_following(self, username: str) -> List[Dict[str, Any]]:
        """Get all following for a user (paginated).
        
        Args:
            username: GitHub username
            
        Returns:
            List of all following user dicts
        """
        all_following = []
        page = 1
        
        while True:
            following = await self.get_following(username, page=page, per_page=100)
            if not following:
                break
            all_following.extend(following)
            page += 1
            
            # Rate limiting delay
            await asyncio.sleep(Config.API_REQUEST_DELAY)
        
        return all_following
    
    async def follow_user(self, username: str) -> bool:
        """Follow a user (requires PAT with user:follow scope).
        
        Args:
            username: GitHub username to follow
            
        Returns:
            True if successful
        """
        if not self.pat:
            print("❌ PAT token required for follow action")
            return False
        
        result = await self._request('PUT', f'/user/following/{username}')
        return result is not None
    
    async def unfollow_user(self, username: str) -> bool:
        """Unfollow a user (requires PAT with user:follow scope).
        
        Args:
            username: GitHub username to unfollow
            
        Returns:
            True if successful
        """
        if not self.pat:
            print("❌ PAT token required for unfollow action")
            return False
        
        result = await self._request('DELETE', f'/user/following/{username}')
        return result is not None
    
    async def get_authenticated_user(self) -> Optional[Dict[str, Any]]:
        """Get the authenticated user's own profile (requires PAT)."""
        return await self._request('GET', '/user')

    async def get_user_repos(self, username: str, page: int = 1, per_page: int = 100) -> List[Dict[str, Any]]:
        """Get public repos for a user."""
        result = await self._request('GET', f'/users/{username}/repos',
                                     params={'page': page, 'per_page': per_page,
                                             'sort': 'pushed', 'type': 'owner'})
        return result if result else []

    async def get_all_user_repos(self, username: str) -> List[Dict[str, Any]]:
        """Get all public repos (paginated)."""
        all_repos, page = [], 1
        while True:
            batch = await self.get_user_repos(username, page=page)
            if not batch:
                break
            all_repos.extend(batch)
            page += 1
            await asyncio.sleep(Config.API_REQUEST_DELAY)
        return all_repos

    async def get_repo_contributors(self, owner: str, repo: str, per_page: int = 100) -> List[Dict[str, Any]]:
        """Get contributors for a repo."""
        result = await self._request('GET', f'/repos/{owner}/{repo}/contributors',
                                     params={'per_page': per_page})
        return result if result else []

    async def get_rate_limit(self) -> Optional[Dict[str, Any]]:
        """Fetch live rate limit from /rate_limit endpoint (free — doesn't consume quota)."""
        return await self._request('GET', '/rate_limit')

    def get_rate_limit_status(self) -> Dict[str, Any]:
        """Get current rate limit status.
        
        Returns:
            Dict with rate limit info
        """
        return {
            'remaining': self.rate_limit_remaining,
            'limit': self.rate_limit_limit,
            'reset': self.rate_limit_reset,
            'reset_time': datetime.fromtimestamp(self.rate_limit_reset).isoformat() if self.rate_limit_reset else None,
            'requests_made': self.requests_made
        }
