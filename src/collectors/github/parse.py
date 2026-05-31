"""Pure parsing/validation helpers for the github collector.

Extracted from the collector class (STAGE 2 of the per-package refactor). Pure,
side-effect-free functions — no ``self``, no I/O — so they unit-test trivially
and carry zero deploy risk. The collector keeps thin staticmethod shims.
"""
from __future__ import annotations


def get_pat_display(pat: str) -> str:
    """Mask a PAT for safe display: ``ghp_xxxx****...****yyyy``."""
    if not pat or len(pat) < 8:
        return "****"
    return f"{pat[:4]}****...****{pat[-4:]}"


def validate_pat_format(pat: str) -> bool:
    """Sanity-check a PAT looks like a real GitHub token."""
    if not pat or len(pat) <= 20:
        return False
    return any(pat.startswith(p) for p in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"))
