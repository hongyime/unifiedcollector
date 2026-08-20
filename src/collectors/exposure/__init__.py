"""Defensive exposure-audit collector.

The normal ``search`` collector is broad public discovery. ``exposure`` is the
separate, explicitly gated lane for sensitive dorks such as exposed backups,
configuration files, open directories, and accidental credential pages.
"""
from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.collectors.search import SearchCollector


logger = logging.getLogger(__name__)
DEFAULT_DORKS_FILE = (
    Path(__file__).resolve().parents[3] / "config" / "sources" / "exposure.dorks"
)
_SECRET_WORD_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
    r"\b\s*[:=]\s*([^\s'\";&]{4,})"
)


@dataclass(frozen=True)
class ExposureGate:
    exact_domains: frozenset[str]
    wildcard_domains: frozenset[str]
    regexes: tuple[re.Pattern[str], ...]


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalise_scope(scope: str) -> str:
    text = scope.strip().lower()
    if "://" in text:
        parsed = urlparse(text)
        text = parsed.netloc or parsed.path
    return text.strip().strip("/")


def _compile_regexes(values: list[str]) -> tuple[re.Pattern[str], ...]:
    out: list[re.Pattern[str]] = []
    for value in values:
        try:
            out.append(re.compile(value, re.IGNORECASE))
        except re.error:
            continue
    return tuple(out)


def build_gate(targets: list[str]) -> ExposureGate:
    exact: set[str] = set()
    wildcard: set[str] = set()
    regex_values = _csv(os.getenv("EXPOSURE_ALLOWED_REGEX", ""))

    for item in _csv(os.getenv("EXPOSURE_ALLOWED_DOMAINS", "")):
        if item.startswith("regex:"):
            regex_values.append(item[len("regex:"):])
        elif item.startswith("*."):
            wildcard.add(item[2:].lower())
        elif item:
            exact.add(_normalise_scope(item))

    for target in targets:
        target = target.strip()
        if target.startswith("regex:"):
            regex_values.append(target[len("regex:"):])

    return ExposureGate(
        exact_domains=frozenset(domain for domain in exact if domain),
        wildcard_domains=frozenset(domain for domain in wildcard if domain),
        regexes=_compile_regexes(regex_values),
    )


def is_scope_allowed(scope: str, gate: ExposureGate) -> bool:
    scope = _normalise_scope(scope)
    if not scope:
        return False
    if scope in gate.exact_domains:
        return True
    for suffix in gate.wildcard_domains:
        if scope == suffix or scope.endswith(f".{suffix}"):
            return True
    return any(regex.search(scope) for regex in gate.regexes)


def redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)


def classify_exposure(query: str, hit: dict[str, Any]) -> tuple[str, str, float, bool]:
    text = " ".join(
        str(part or "")
        for part in (query, hit.get("url"), hit.get("title"), hit.get("snippet"))
    ).lower()
    secret_like = _SECRET_WORD_RE.search(text) is not None
    if any(marker in text for marker in ("/.git", "filename:.git", "git-credentials")):
        return "exposed_git", "high", 0.85, secret_like
    if any(marker in text for marker in ("ext:sql", "mysql dump", ".sql", ".db", ".mdb")):
        return "database_or_dump", "high", 0.8, secret_like
    if any(marker in text for marker in ("ext:bak", ".bak", ".backup", ".old", "backup")):
        return "backup_file", "high", 0.75, secret_like
    if any(marker in text for marker in (".env", "wp-config", "config", "credential", "private_key")):
        return "config_or_secret_file", "critical" if secret_like else "high", 0.8, secret_like
    if "index of" in text or "intitle:index.of" in text:
        return "directory_listing", "medium", 0.65, secret_like
    if any(marker in text for marker in ("sql syntax", "php warning", "php error", "php parse")):
        return "application_error", "medium", 0.7, secret_like
    if "login" in text or "signin" in text or "signup" in text:
        return "login_or_admin_surface", "low", 0.45, secret_like
    return "sensitive_search_hit", "medium" if secret_like else "low", 0.5, secret_like


class ExposureCollector(SearchCollector):
    SOURCE_NAME = "exposure"

    def __init__(self):
        super().__init__()
        self._download_images = os.getenv("EXPOSURE_FETCH_EVIDENCE", "0") == "1"
        self._download_docs = False
        self._download_videos = False
        self._spider_pages = os.getenv("EXPOSURE_SPIDER_PAGES", "0") == "1"
        self._dorks_file = Path(os.getenv("EXPOSURE_DORKS_FILE", str(DEFAULT_DORKS_FILE)))
        self._max_queries = int(os.getenv("EXPOSURE_MAX_QUERIES_PER_CYCLE", "100"))

    def _load_dorks(self) -> list[str]:
        if not self._dorks_file.is_file():
            return []
        out: list[str] = []
        seen: set[str] = set()
        for raw in self._dorks_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            if "[TARGET]" not in line:
                continue
            if line not in seen:
                seen.add(line)
                out.append(line)
        return out

    async def collect(self, targets: list[str]):
        if os.getenv("EXPOSURE_ENABLED", "0") != "1":
            logger.info("exposure disabled; set EXPOSURE_ENABLED=1 to run")
            return
        gate = build_gate(targets)
        scopes = [
            _normalise_scope(target)
            for target in targets
            if target.strip() and not target.strip().startswith("regex:")
        ]
        scopes = [scope for scope in scopes if is_scope_allowed(scope, gate)]
        if not scopes:
            logger.warning("exposure has no concrete target scopes allowed by gates; skipping")
            return

        dorks = self._load_dorks()
        if not dorks:
            logger.warning("exposure dork file is empty or missing: %s", self._dorks_file)
            return

        queries: list[str] = []
        for scope in scopes:
            for dork in dorks:
                queries.append(dork.replace("[TARGET]", scope))
                if len(queries) >= self._max_queries:
                    break
            if len(queries) >= self._max_queries:
                break
        await super().collect(queries)

    async def _upsert_result(self, query: str, hit: dict) -> bool:
        hit = dict(hit)
        hit["title"] = redact_text(hit.get("title"))
        hit["snippet"] = redact_text(hit.get("snippet"))
        inserted = await super()._upsert_result(query, hit)
        await self._upsert_exposure_finding(query, hit, inserted=inserted)
        return inserted

    async def _upsert_exposure_finding(self, query: str, hit: dict, *, inserted: bool) -> None:
        if self.pool is None or not hit.get("url"):
            return
        category, severity, confidence, secret_like = classify_exposure(query, hit)
        domain = hit.get("domain") or urlparse(hit["url"]).netloc
        target_scope = self._target_scope_for_query(query)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO exposure_findings
                    (target_scope, query, url, domain, category, severity, confidence,
                     title, snippet, detected_secret, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (url, query) DO UPDATE SET
                    severity = EXCLUDED.severity,
                    confidence = EXCLUDED.confidence,
                    title = EXCLUDED.title,
                    snippet = EXCLUDED.snippet,
                    detected_secret = EXCLUDED.detected_secret,
                    metadata = exposure_findings.metadata || EXCLUDED.metadata,
                    collected_at = NOW()
                """,
                target_scope,
                query,
                hit["url"],
                domain,
                category,
                severity,
                confidence,
                hit.get("title"),
                hit.get("snippet"),
                secret_like,
                {"engine": hit.get("engine"), "search_result_inserted": inserted},
            )

    @staticmethod
    def _target_scope_for_query(query: str) -> str | None:
        match = re.search(r"site:([^\s)]+)", query, flags=re.IGNORECASE)
        if match:
            return _normalise_scope(match.group(1).strip('"'))
        return None


__all__ = [
    "ExposureCollector",
    "build_gate",
    "classify_exposure",
    "is_scope_allowed",
    "redact_text",
]
