import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_PATTERN_LEN = 512

DEFAULT_BLOCKLIST = [
    "*/login*", "*/signin*", "*/signup*", "*/register*", "*/auth/*",
    "*/admin/*", "*/wp-admin/*", "*/dashboard/*",
    "*/cart*", "*/checkout*", "*/payment*", "*/order*",
    "*.facebook.com/*", "*.twitter.com/*", "*.instagram.com/*",
    "*.tiktok.com/*", "*.linkedin.com/*",
    "*accounts.google.com/*", "*login.microsoftonline.com/*",
    "*/api/*", "*/_next/*", "*/static/*",
    "*.css", "*.js", "*.woff*", "*.ttf",
    "*?utm_*", "*#*",
]


def _wildcard_to_regex(pattern: str) -> str | None:
    if len(pattern) > MAX_PATTERN_LEN:
        logger.warning("Pattern too long (%d chars), skipping: %.50s...", len(pattern), pattern)
        return None
    escaped = re.escape(pattern)
    regex = escaped.replace(r"\*", ".*").replace(r"\?", ".")
    return f"^{regex}$"


class URLFilter:

    def __init__(self, allow_patterns: list[str] | None = None,
                 block_patterns: list[str] | None = None,
                 use_default_blocklist: bool = True):
        self._allow: list[re.Pattern] = []
        self._block: list[re.Pattern] = []

        if allow_patterns:
            for p in allow_patterns:
                self._compile_and_add(p, self._allow)

        block_list = list(block_patterns or [])
        if use_default_blocklist:
            block_list.extend(DEFAULT_BLOCKLIST)
        for p in block_list:
            self._compile_and_add(p, self._block)

    def _compile_and_add(self, pattern: str, target: list[re.Pattern]):
        regex = _wildcard_to_regex(pattern.strip())
        if regex is None:
            return
        try:
            compiled = re.compile(regex, re.IGNORECASE)
            target.append(compiled)
        except re.error as e:
            logger.warning("Invalid filter pattern '%s': %s", pattern, e)

    def is_allowed(self, url: str) -> tuple[bool, str]:
        if self._allow:
            for pat in self._allow:
                if pat.match(url):
                    return True, "allowlist"
            return False, "not in allowlist"

        for pat in self._block:
            if pat.match(url):
                return False, f"blocked: {pat.pattern[:80]}"

        return True, "ok"

    @classmethod
    def from_env(cls, allow_var: str = "", block_var: str = "") -> "URLFilter":
        import os
        allow_val = os.getenv(allow_var, "") if allow_var else ""
        block_val = os.getenv(block_var, "") if block_var else ""
        allow = [p.strip() for p in allow_val.split(",") if p.strip()] if allow_val else None
        block = [p.strip() for p in block_val.split(",") if p.strip()] if block_val else None
        return cls(allow_patterns=allow, block_patterns=block)
