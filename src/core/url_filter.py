import logging
import os
import re
from pathlib import Path
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


_MAX_WILDCARDS = 8
_RAW_REGEX_PREFIXES = ("regex:", "re:")
_POLICY_ACTIONS = {
    "allow",
    "allow_regex",
    "allow-re",
    "allow_re",
    "block",
    "block_regex",
    "block-re",
    "block_re",
}
_DANGEROUS_REGEX_RE = re.compile(r"\((?:\?:)?[^)]*[*+][^)]*\)\s*(?:[*+{])|\\[1-9]")


def _glob_fragment_to_regex(value: str) -> str:
    escaped = re.escape(value)
    return escaped.replace(r"\*", ".*?").replace(r"\?", ".")


def _raw_regex_to_regex(pattern: str) -> str | None:
    raw = pattern.split(":", 1)[1].strip()
    if len(raw) > MAX_PATTERN_LEN:
        logger.warning("Regex pattern too long (%d chars), skipping: %.50s...", len(raw), raw)
        return None
    lowered = raw.lower()
    if not (
        lowered.startswith("^https?://")
        or lowered.startswith("^http://")
        or lowered.startswith("^https://")
    ):
        logger.warning("URL regex must be anchored to http(s), skipping: %.80s...", raw)
        return None
    if _DANGEROUS_REGEX_RE.search(raw):
        logger.warning("URL regex has unsafe nested quantifiers/backrefs, skipping: %.80s...", raw)
        return None
    return raw


def _host_only_url_pattern_to_regex(pattern: str) -> str | None:
    parsed = urlparse(pattern)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None
    if parsed.path not in {"", "/"}:
        return None
    host = parsed.netloc.strip()
    if not host:
        return None
    scheme = re.escape(parsed.scheme)
    host_regex = _glob_fragment_to_regex(host)
    return rf"^{scheme}://{host_regex}(?::\d+)?(?:/.*)?$"


def _wildcard_to_regex(pattern: str) -> str | None:
    pattern = pattern.strip()
    if pattern.lower().startswith(_RAW_REGEX_PREFIXES):
        return _raw_regex_to_regex(pattern)
    if len(pattern) > MAX_PATTERN_LEN:
        logger.warning("Pattern too long (%d chars), skipping: %.50s...", len(pattern), pattern)
        return None
    # Cap wildcards to keep regex matching linear-bounded against adversarial
    # URLs. Multiple `.*` in the same anchored pattern can backtrack
    # super-linearly; 8 is well above any realistic legitimate pattern.
    if pattern.count("*") > _MAX_WILDCARDS:
        logger.warning(
            "Pattern has %d wildcards (max %d), skipping: %.80s...",
            pattern.count("*"), _MAX_WILDCARDS, pattern,
        )
        return None
    host_only = _host_only_url_pattern_to_regex(pattern)
    if host_only:
        return host_only
    # Use non-greedy `.*?` to bound backtracking on adversarial inputs.
    regex = _glob_fragment_to_regex(pattern)
    return f"^{regex}$"


def _csv_patterns(raw: str | None) -> list[str] | None:
    values = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return values or None


def _parse_policy_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("!"):
        value = stripped[1:].strip()
        return ("block", value) if value else None

    head, sep, tail = stripped.partition(" ")
    if sep and head.strip().lower().rstrip(":") in _POLICY_ACTIONS:
        return head.strip().lower().rstrip(":"), tail.strip()

    head, sep, tail = stripped.partition(":")
    action = head.strip().lower()
    if sep and action in _POLICY_ACTIONS:
        return action, tail.strip()

    logger.warning("Invalid URL policy line (expected allow/block prefix), skipping: %.80s", stripped)
    return None


def _read_policy_file(path: str | None) -> tuple[list[str], list[str]]:
    if not path:
        return [], []
    policy_path = Path(path)
    if not policy_path.exists():
        return [], []

    allow: list[str] = []
    block: list[str] = []
    try:
        lines = policy_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("Could not read URL policy file %s: %s", policy_path, exc)
        return [], []

    for line in lines:
        parsed = _parse_policy_line(line)
        if not parsed:
            continue
        action, value = parsed
        if not value:
            continue
        if action in {"allow_regex", "allow-re", "allow_re"}:
            allow.append(f"regex:{value}")
        elif action in {"block_regex", "block-re", "block_re"}:
            block.append(f"regex:{value}")
        elif action == "allow":
            allow.append(value)
        elif action == "block":
            block.append(value)
    return allow, block


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
        for pat in self._block:
            if pat.match(url):
                return False, f"blocked: {pat.pattern[:80]}"

        if self._allow:
            for pat in self._allow:
                if pat.match(url):
                    return True, "allowlist"
            return False, "not in allowlist"

        return True, "ok"

    @classmethod
    def from_policy_file(cls, policy_file: str | None) -> "URLFilter":
        allow, block = _read_policy_file(policy_file)
        return cls(allow_patterns=allow or None, block_patterns=block or None)

    @classmethod
    def from_env(
        cls,
        allow_var: str = "",
        block_var: str = "",
        *,
        policy_file_var: str = "",
        policy_file_default: str = "",
    ) -> "URLFilter":
        allow_val = os.getenv(allow_var, "") if allow_var else ""
        block_val = os.getenv(block_var, "") if block_var else ""
        allow = _csv_patterns(allow_val) or []
        block = _csv_patterns(block_val) or []
        if policy_file_var:
            policy_file = os.getenv(policy_file_var, "") or policy_file_default
            file_allow, file_block = _read_policy_file(policy_file)
            allow.extend(file_allow)
            block.extend(file_block)
        return cls(allow_patterns=allow or None, block_patterns=block or None)
