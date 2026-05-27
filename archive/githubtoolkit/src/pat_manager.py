"""GitHub PAT manager — stores tokens in .env as GITHUB_PAT=tok1,tok2,..."""
import os
from pathlib import Path
from typing import Optional, List
from dotenv import dotenv_values

from src.config import Config

_ENV_PATH = Config.BASE_DIR / ".env"
_KEY = "GITHUB_PAT"


def _read_env_file() -> List[str]:
    """Read .env and return lines."""
    if not _ENV_PATH.exists():
        return []
    return _ENV_PATH.read_text(encoding='utf-8').splitlines()


def _write_env_key(key: str, value: str):
    """Update or append key=value in .env, then refresh os.environ."""
    lines = _read_env_file()
    updated = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped == key:
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding='utf-8')
    os.environ[key] = value  # keep in-process env in sync


def _pats_from_env() -> List[str]:
    """Read GITHUB_PAT from .env file (always fresh, not cached os.environ)."""
    vals = dotenv_values(_ENV_PATH)
    raw = vals.get(_KEY, "").strip()
    return [t.strip() for t in raw.split(",") if t.strip()]


class PATManager:
    """Manages GitHub PAT tokens stored in .env as GITHUB_PAT=tok1,tok2,..."""

    def store_pat(self, pat: str) -> bool:
        """Add PAT to list if not already present. Saves to .env."""
        try:
            pats = _pats_from_env()
            if pat in pats:
                print(f"ℹ️  PAT already stored ({len(pats)} total)")
                return True
            pats.append(pat)
            _write_env_key(_KEY, ",".join(pats))
            print(f"✅ PAT saved to .env ({len(pats)} total)")
            return True
        except Exception as e:
            print(f"❌ Failed to save PAT: {e}")
            return False

    def load_pat(self) -> Optional[str]:
        """Return first (primary) PAT, or None."""
        pats = _pats_from_env()
        if not pats:
            print("❌ No PAT configured. Go to Authentication > Add PAT token.")
        return pats[0] if pats else None

    def load_all_pats(self) -> List[str]:
        """Return all stored PATs."""
        return _pats_from_env()

    def delete_pat(self, index: int = 0) -> bool:
        """Remove PAT at index. Saves updated list to .env."""
        try:
            pats = _pats_from_env()
            if not pats or not (0 <= index < len(pats)):
                print("❌ Invalid index")
                return False
            removed = pats.pop(index)
            _write_env_key(_KEY, ",".join(pats))
            print(f"✅ Removed: {self.get_pat_display(removed)}")
            return True
        except Exception as e:
            print(f"❌ Failed to remove PAT: {e}")
            return False

    def list_pats(self) -> List[str]:
        """Return list of masked PAT displays."""
        return [self.get_pat_display(p) for p in _pats_from_env()]

    def get_pat_display(self, pat: str) -> str:
        if len(pat) < 8:
            return "****"
        return f"{pat[:4]}****...****{pat[-4:]}"

    def validate_pat_format(self, pat: str) -> bool:
        valid_prefixes = ['ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_']
        return any(pat.startswith(p) for p in valid_prefixes) and len(pat) > 20
