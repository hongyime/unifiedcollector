"""Small CDP helpers for the UnifiedCollector extension control tab."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any


PRIMARY_EXTENSION_ID = "pkmdmcklnjdeocoeigmlakhomhhcpafb"


def cdp_base() -> str:
    port = os.getenv("UC_CHROME_CDP_PORT", "9333").strip() or "9333"
    return f"http://127.0.0.1:{port}"


def read_text(path: str, timeout: int = 10) -> str:
    with urllib.request.urlopen(f"{cdp_base()}{path}", timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def read_json(path: str, timeout: int = 10) -> Any:
    return json.loads(read_text(path, timeout=timeout))


def cdp_put(path: str, timeout: int = 10) -> Any:
    request = urllib.request.Request(f"{cdp_base()}{path}", method="PUT")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")


def list_targets() -> list[dict[str, Any]]:
    targets = read_json("/json/list")
    return targets if isinstance(targets, list) else []


def is_control_tab_url(url: str | None) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except Exception:
        return False
    return parsed.scheme == "chrome-extension" and parsed.path == "/tabs.html"


def control_tab_targets() -> list[dict[str, Any]]:
    return [
        target
        for target in list_targets()
        if target.get("type") == "page" and is_control_tab_url(target.get("url"))
    ]


def primary_control_tab_targets() -> list[dict[str, Any]]:
    primary_base = f"chrome-extension://{PRIMARY_EXTENSION_ID}/tabs.html"
    return [
        target
        for target in control_tab_targets()
        if str(target.get("url") or "").startswith(primary_base)
    ]


def _control_rank(target: dict[str, Any]) -> tuple[int, int, str]:
    url = str(target.get("url") or "")
    primary_base = f"chrome-extension://{PRIMARY_EXTENSION_ID}/tabs.html"
    if url == primary_base:
        primary = 0
    elif url.startswith(primary_base):
        primary = 1
    elif is_control_tab_url(url):
        primary = 2
    else:
        primary = 3
    active = 0 if target.get("attached") else 1
    return (primary, active, str(target.get("id") or ""))


def preferred_control_tab(primary_only: bool = False) -> dict[str, Any] | None:
    tabs = sorted(primary_control_tab_targets() if primary_only else control_tab_targets(), key=_control_rank)
    return tabs[0] if tabs else None


def activate_target(target_id: str) -> None:
    encoded = urllib.parse.quote(str(target_id), safe="")
    read_text(f"/json/activate/{encoded}", timeout=5)


def close_target(target_id: str) -> None:
    encoded = urllib.parse.quote(str(target_id), safe="")
    read_text(f"/json/close/{encoded}", timeout=5)


def close_duplicate_control_tabs(keep_id: str | None = None) -> int:
    tabs = sorted(control_tab_targets(), key=_control_rank)
    if not tabs:
        return 0
    primary = sorted(primary_control_tab_targets(), key=_control_rank)
    keep = keep_id if keep_id is not None else (str(primary[0].get("id") or "") if primary else "")
    closed = 0
    for target in tabs:
        target_id = str(target.get("id") or "")
        if not target_id or target_id == keep:
            continue
        try:
            close_target(target_id)
            closed += 1
        except Exception:
            pass
    return closed


def open_or_activate_control_tab(settle_seconds: float = 0.0) -> dict[str, Any] | None:
    target = preferred_control_tab(primary_only=True)
    if target and target.get("id"):
        try:
            activate_target(str(target["id"]))
        except Exception:
            pass
        close_duplicate_control_tabs(str(target["id"]))
        return target

    url = f"chrome-extension://{PRIMARY_EXTENSION_ID}/tabs.html"
    cdp_put("/json/new?" + urllib.parse.quote(url, safe=":/"))
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    target = preferred_control_tab(primary_only=True)
    if target and target.get("id"):
        close_duplicate_control_tabs(str(target["id"]))
    else:
        close_duplicate_control_tabs()
    return target
