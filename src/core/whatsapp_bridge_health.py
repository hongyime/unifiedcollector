"""Read-only WhatsApp bridge health helpers.

Container health only proves the Node process answers HTTP. Collection health
needs the bridge session state: at least one bridge must be paired and ready.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from typing import Any


def _bridge_base(bridge: str) -> str:
    return os.getenv(f"WA_BRIDGE_{bridge}_URL", f"http://wa-bridge-{bridge}:3001")


def _fetch_json(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_bridge_health(bridge: str, timeout: float) -> dict[str, Any]:
    base = _bridge_base(bridge)
    try:
        body = _fetch_json(f"{base}/health", timeout)
        return {"bridge": bridge, "ok": True, **body}
    except Exception as health_exc:  # noqa: BLE001 - health must be best effort
        try:
            livez = _fetch_json(f"{base}/livez", min(timeout, 2))
            return {
                "bridge": bridge,
                "ok": True,
                "status": "health_timeout_alive",
                "error": str(health_exc),
                **livez,
            }
        except Exception as live_exc:  # noqa: BLE001 - health must be best effort
            return {
                "bridge": bridge,
                "ok": False,
                "status": "unreachable",
                "error": str(live_exc),
                "health_error": str(health_exc),
            }


async def fetch_whatsapp_bridge_health(timeout: float = 5) -> list[dict[str, Any]]:
    """Return bridge health for both WhatsApp slots without raising."""
    return list(await asyncio.gather(
        asyncio.to_thread(_fetch_bridge_health, "1", timeout),
        asyncio.to_thread(_fetch_bridge_health, "2", timeout),
    ))


def summarize_whatsapp_bridge_health(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize bridge states for collection health.

    Returned status:
      paired      - every reachable/expected bridge is ready/connected.
      partial     - at least one bridge is ready, but another slot needs QR/session attention.
      unpaired    - bridges are reachable but waiting for QR/session pairing.
      unreachable - no bridge endpoint responded.
      degraded    - reachable, but not paired and not clearly awaiting QR.
    """
    total = len(states)
    reachable = [s for s in states if s.get("ok")]
    ready = [
        s for s in reachable
        if s.get("whatsapp_ready") is True or s.get("ready") is True or s.get("connected") is True
    ]
    qr_waiting_statuses = {
        "awaiting_scan",
        "connecting_unpaired",
        "pairing",
        "qr",
        "qr_expired",
        "refreshing_qr",
    }
    qr_waiting = [
        s for s in reachable
        if s.get("qr_available") is True
        or str(s.get("status") or "").lower() in qr_waiting_statuses
        or "qr" in str(s.get("last_disconnect_reason") or "").lower()
    ]
    waiting_labels = [
        str(s.get("session_name") or s.get("bridge") or "?")
        for s in qr_waiting
    ]

    if ready:
        if qr_waiting or len(ready) < total:
            detail = f"{len(ready)} WhatsApp bridge slot(s) paired and ready"
            if waiting_labels:
                detail += f"; {len(qr_waiting)} slot(s) waiting for QR/session pairing: {', '.join(waiting_labels)}"
            elif len(ready) < total:
                detail += f"; {total - len(ready)} slot(s) not ready"
            detail += ". Collection continues through the paired slot(s), but coverage is incomplete."
            return {
                "status": "partial",
                "ready_count": len(ready),
                "reachable_count": len(reachable),
                "waiting_count": len(qr_waiting),
                "total": total,
                "detail": detail,
            }
        return {
            "status": "paired",
            "ready_count": len(ready),
            "reachable_count": len(reachable),
            "waiting_count": 0,
            "total": total,
            "detail": f"{len(ready)} WhatsApp bridge slot(s) paired and ready.",
        }
    if not reachable:
        return {
            "status": "unreachable",
            "ready_count": 0,
            "reachable_count": 0,
            "waiting_count": 0,
            "total": total,
            "detail": "No WhatsApp bridge HTTP endpoint is reachable.",
        }
    if qr_waiting:
        return {
            "status": "unpaired",
            "ready_count": 0,
            "reachable_count": len(reachable),
            "waiting_count": len(qr_waiting),
            "total": total,
            "detail": "WhatsApp bridges are waiting for QR pairing; no live WhatsApp messages will collect until a bridge is paired.",
        }
    return {
        "status": "degraded",
        "ready_count": 0,
        "reachable_count": len(reachable),
        "waiting_count": 0,
        "total": total,
        "detail": "WhatsApp bridge endpoints respond, but no bridge reports a ready collection session.",
    }
