"""Media-path validation and thumbnail placeholder helpers for dashboard routes."""
from __future__ import annotations

import html
import sys
import uuid as _uuid
from pathlib import Path

from fastapi import HTTPException, Response

from src.core.vault import VAULT_ROOT


def _parse_media_uuid(media_id: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(media_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Invalid media id")


def _is_relative_to_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_media_roots() -> list[Path]:
    from src.core.drive_check import DRIVE_PATH as _DRIVE_PATH

    root = sys.modules.get("src.dashboard.api")
    vault_root = getattr(root, "VAULT_ROOT", VAULT_ROOT) if root else VAULT_ROOT
    roots = [Path(_DRIVE_PATH).resolve(), (Path(vault_root) / "media").resolve()]
    return list(dict.fromkeys(roots))


def _resolve_media_path(file_path_str: str) -> Path:
    if not file_path_str:
        raise HTTPException(status_code=404, detail="Media path missing")
    file_path = Path(file_path_str).resolve()
    if not any(_is_relative_to_path(file_path, root) for root in _allowed_media_roots()):
        raise HTTPException(status_code=403, detail="Path outside media roots")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return file_path


def _thumbnail_placeholder(label: str, detail: str = "") -> Response:
    safe_label = html.escape((label or "media").upper()[:24])
    safe_detail = html.escape((detail or "preview unavailable")[:64])
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300" role="img" aria-label="{safe_label}">
<rect width="300" height="300" fill="#111827"/><rect x="18" y="18" width="264" height="264" rx="10" fill="#1f2937" stroke="#374151" stroke-width="2"/>
<circle cx="150" cy="122" r="34" fill="#4b5563"/><path d="M142 104 L172 122 L142 140 Z" fill="#e5e7eb"/>
<text x="150" y="190" text-anchor="middle" fill="#f9fafb" font-family="Arial, sans-serif" font-size="24" font-weight="700">{safe_label}</text>
<text x="150" y="218" text-anchor="middle" fill="#9ca3af" font-family="Arial, sans-serif" font-size="13">{safe_detail}</text></svg>'''
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=300"})

__all__ = [
    "_allowed_media_roots",
    "_is_relative_to_path",
    "_parse_media_uuid",
    "_resolve_media_path",
    "_thumbnail_placeholder",
]
