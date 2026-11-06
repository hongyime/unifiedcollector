"""Authenticated browser callbacks for recovery-profile state."""
from aiohttp import web

from .cors import _cors


async def recovery_clean_pass_handler(request):
    """Accept a bounded post-expiry Instagram observation as one clean pass."""
    from . import _safe_json
    from src.core.recovery_profiles import record_clean_recovery_pass

    body = await _safe_json(request)
    source = str(body.get("source") or "").strip().lower()
    if source != "instagram":
        return _cors(web.json_response({"ok": False, "error": "unsupported_source"}, status=400))
    account = str(body.get("account") or "").strip() or None
    profile = await record_clean_recovery_pass(request.app.get("pool"), source, account=account)
    if profile is None:
        return _cors(web.json_response({"ok": False, "error": "recovery_profile_missing"}, status=404))
    return _cors(web.json_response({
        "ok": True,
        "state": profile.get("state"),
        "clean_passes": profile.get("clean"),
        "normal": profile.get("state") == "normal",
    }))
