"""TikTok cookie management with validation and dynamic token handling.

This module provides enhanced cookie management for TikTok authentication,
including validation of required cookies and detection of missing tokens
that may cause anti-bot protection to trigger.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Set
from datetime import datetime

logger = logging.getLogger('uttk.cookie_manager')


class TikTokCookieManager:
    """Manages TikTok cookies with validation and token checking.
    
    TikTok requires multiple cookies for successful authentication:
    - sessionid/sessionid_ss: Primary authentication tokens
    - sid_tt/sid_guard: Session identifiers
    - uid_tt/uid_tt_ss: User identifiers
    - msToken: Dynamic token (refreshes frequently, critical for API access)
    - tt_chain_token: Chain token for request validation
    - s_v_web_id: Web visitor ID
    - ttwid: TikTok web ID
    
    Missing any of these may trigger anti-bot protection.
    """
    
    # Critical cookies required for authenticated access
    REQUIRED_COOKIES = {
        'sessionid',        # Primary authentication
        'sid_tt',           # Session identifier
        'uid_tt',           # User identifier
        'msToken',          # Dynamic token (critical for API)
        'tt_chain_token',   # Chain token
        's_v_web_id',       # Web visitor ID
        'ttwid',            # TikTok web ID
    }
    
    # Important cookies that improve success rate
    RECOMMENDED_COOKIES = {
        's_v_web_id',       # Web ID
        'ttwid',            # TikTok web ID
    }
    
    def __init__(self):
        """Initialize cookie manager."""
        pass

    @property
    def required_cookies(self) -> set:
        """Set of required cookie names."""
        return self.REQUIRED_COOKIES

    @property
    def recommended_cookies(self) -> set:
        """Set of recommended cookie names."""
        return self.RECOMMENDED_COOKIES

    def validate_cookies(self, cookies_file: Path) -> Dict[str, any]:
        """Validate TikTok cookies file and check for required tokens.
        
        Args:
            cookies_file: Path to Netscape-format cookies file
            
        Returns:
            Dictionary with validation results:
            {
                'valid': bool,
                'exists': bool,
                'total_cookies': int,
                'cookies_found': Set[str],
                'required_present': List[str],
                'required_missing': List[str],
                'recommended_missing': Set[str],
                'expired_cookies': List[str],
                'warnings': List[str],
                'error': Optional[str]
            }
        """
        result = {
            'valid': False,
            'exists': False,
            'total_cookies': 0,
            'cookies_found': set(),
            'required_present': [],
            'required_missing': [],
            'recommended_missing': set(),
            'expired_cookies': [],
            'warnings': [],
            'error': None
        }
        
        # Check if file exists
        if not cookies_file.exists():
            result['warnings'].append(f"Cookies file not found: {cookies_file}")
            result['error'] = f"Cookies file not found: {cookies_file}"
            return result
        
        result['exists'] = True
        
        # Check if file is empty
        if cookies_file.stat().st_size == 0:
            result['error'] = "Cookies file is empty"
            result['required_missing'] = sorted(self.REQUIRED_COOKIES)
            return result
        
        # Parse cookies file
        try:
            cookies_found = self._parse_cookies_file(cookies_file)
            result['cookies_found'] = cookies_found
            result['total_cookies'] = len(cookies_found)
            
            # Check for required cookies
            missing = self.REQUIRED_COOKIES - cookies_found
            present = self.REQUIRED_COOKIES & cookies_found
            result['required_missing'] = sorted(missing)
            result['required_present'] = sorted(present)
            result['recommended_missing'] = self.RECOMMENDED_COOKIES - cookies_found
            
            # Check expiration (basic check)
            expired = self._check_expired_cookies(cookies_file)
            result['expired_cookies'] = expired
            
            # Generate warnings
            if result['required_missing']:
                result['warnings'].append(
                    f"Missing required cookies: {', '.join(sorted(result['required_missing']))}"
                )
            
            if result['recommended_missing']:
                result['warnings'].append(
                    f"Missing recommended cookies: {', '.join(sorted(result['recommended_missing']))}"
                )
            
            if expired:
                result['warnings'].append(
                    f"Expired cookies detected: {', '.join(expired[:3])}"
                    + (f" and {len(expired) - 3} more" if len(expired) > 3 else "")
                )
            
            # Determine if valid (has all required cookies)
            result['valid'] = len(result['required_missing']) == 0
            
        except Exception as e:
            result['error'] = f"Failed to parse cookies file: {e}"
            logger.error(f"Cookie validation error: {e}")
        
        return result
    
    def _parse_cookies_file(self, cookies_file: Path) -> Set[str]:
        """Parse Netscape-format cookies file and extract cookie names.
        
        Args:
            cookies_file: Path to cookies file
            
        Returns:
            Set of cookie names found in file
        """
        cookies_found = set()
        
        with cookies_file.open('r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                
                # Skip comments and empty lines
                if not line or line.startswith('#'):
                    continue
                
                # Netscape format: domain, flag, path, secure, expiration, name, value
                parts = line.split('\t')
                if len(parts) >= 7:
                    cookie_name = parts[5].strip()
                    if cookie_name:
                        cookies_found.add(cookie_name)
        
        return cookies_found
    
    def _check_expired_cookies(self, cookies_file: Path) -> List[str]:
        """Check for expired cookies in file.
        
        Args:
            cookies_file: Path to cookies file
            
        Returns:
            List of expired cookie names
        """
        expired = []
        current_time = int(datetime.now().timestamp())
        
        try:
            with cookies_file.open('r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    
                    # Netscape format: domain, flag, path, secure, expiration, name, value
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        try:
                            expiration = int(parts[4])
                            cookie_name = parts[5].strip()
                            
                            # Check if expired (expiration of 0 means session cookie)
                            if expiration > 0 and expiration < current_time:
                                expired.append(cookie_name)
                        except (ValueError, IndexError):
                            # Skip malformed lines
                            continue
        except Exception as e:
            logger.debug(f"Error checking cookie expiration: {e}")
        
        return expired
    
    def extract_with_metadata(self, browser: str, output_path: Path) -> Path:
        """Extract cookies from browser using gallery-dl.
        
        This is a wrapper around gallery-dl's cookie extraction that
        ensures the output file is created correctly.
        
        Args:
            browser: Browser name (chrome, firefox, edge, safari)
            output_path: Where to save extracted cookies
            
        Returns:
            Path to extracted cookies file
            
        Raises:
            Exception: If extraction fails
        """
        # This method is a placeholder for integration with provider.py
        # The actual extraction is done by gallery-dl in provider.setup_browser_cookies()
        # This method would be called after extraction to validate results
        
        if not output_path.exists():
            raise FileNotFoundError(f"Cookie extraction failed: {output_path} not created")
        
        # Validate extracted cookies
        validation = self.validate_cookies(output_path)
        
        if not validation['valid']:
            logger.warning(f"Extracted cookies may be incomplete: {validation['warnings']}")
        
        return output_path
    
    def get_validation_summary(self, validation_result: Dict) -> str:
        """Generate human-readable summary of validation results.
        
        Args:
            validation_result: Result from validate_cookies()
            
        Returns:
            Formatted summary string
        """
        if not validation_result['exists']:
            return "[X] Cookies file not found"
        
        if validation_result['error']:
            return f"[X] {validation_result['error']}"
        
        lines = []
        
        if validation_result['valid']:
            lines.append("[OK] Cookies file is valid")
        else:
            lines.append("[!] Cookies file has issues")
        
        # Show found cookies count
        found_count = len(validation_result['cookies_found'])
        lines.append(f"[INFO] Found {found_count} cookies")
        
        # Show required status
        required_missing = validation_result['required_missing']
        if required_missing:
            lines.append(f"[X] Missing required: {', '.join(sorted(required_missing))}")
        else:
            lines.append("[OK] All required cookies present")
        
        # Show recommended status
        recommended_missing = validation_result['recommended_missing']
        if recommended_missing:
            lines.append(f"[!] Missing recommended: {', '.join(sorted(recommended_missing))}")
        
        # Show expiration warnings
        if validation_result['expired_cookies']:
            expired_count = len(validation_result['expired_cookies'])
            lines.append(f"[!] {expired_count} expired cookies detected")
        
        return '\n'.join(lines)
