#!/usr/bin/env python3
"""
Soft-reload a set of Chrome tabs via CDP Page.reload.

Reads tmp/browser_tab_audit_result.json, decides which tabs need reload, and issues
Page.reload on each with ignoreCache=false (soft reload). Tabs already healthy
(cs=True cs_version=<extension/manifest.json version>) are skipped.

`x` is expected to be healthy and skipped. Everything else gets reloaded.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CDP_HOST = "http://127.0.0.1:9222"
REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = REPO_ROOT / "tmp" / "browser_tab_audit_result.json"
PLAN_PATH = REPO_ROOT / "tmp" / "browser_tab_reload_plan.json"


def _target_version() -> str:
    with (REPO_ROOT / "extension" / "manifest.json").open(encoding="utf-8") as f:
        return str(json.load(f).get("version") or "").strip()


def _list_targets() -> list[dict]:
    resp = urllib.request.urlopen(f"{CDP_HOST}/json/list", timeout=10)
    return json.loads(resp.read().decode("utf-8"))


def _decide_reload(tab: dict, target_version: str) -> tuple[bool, str]:
    # Hard: main-world unresponsive
    if tab.get("responsive_main") is False:
        return True, "main-world unresponsive"
    # Content script not injected on this version
    if tab.get("cs") is not True:
        return True, "no content script attached"
    if target_version and tab.get("cs_version") != target_version:
        return True, f"cs_version {tab.get('cs_version')} != {target_version}"
    # Memory exceeded 300MB
    if tab.get("heap_mb") is not None and tab["heap_mb"] > 300:
        return True, f"heap {tab['heap_mb']}MB > 300MB"
    return False, "healthy"


def _is_auth_wall(url: str) -> bool:
    """Heuristic: is this URL a login / auth-wall page we shouldn't reload?"""
    if not url:
        return False
    u = url.lower()
    markers = ("/login", "/signin", "/checkpoint", "/challenge", "auth_platform", "recaptcha", "/log_out")
    return any(m in u for m in markers)


def send_reload(ws_url: str, target_id: str, ignore_cache: bool = False, timeout: float = 6.0) -> tuple[bool, str]:
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
    except Exception as e:
        return False, f"connect: {e}"
    try:
        ws.settimeout(timeout)
        payload = {"id": 1, "method": "Page.enable"}
        ws.send(json.dumps(payload))
        # Best-effort: drain events for ~1s for Page.enable response
        deadline = time.time() + timeout
        got_enable = False
        while time.time() < deadline and not got_enable:
            try:
                ws.settimeout(0.5)
                raw = ws.recv()
                m = json.loads(raw)
                if m.get("id") == 1:
                    got_enable = True
                    break
            except (websocket.WebSocketTimeoutException, TimeoutError):
                break

        # Send Page.reload — do not wait for a response (page may reload the socket)
        payload = {"id": 2, "method": "Page.reload", "params": {"ignoreCache": ignore_cache}}
        ws.settimeout(timeout)
        ws.send(json.dumps(payload))
        # Read one message with short timeout to catch the ack
        try:
            ws.settimeout(2.0)
            raw = ws.recv()
            _ = json.loads(raw)
        except (websocket.WebSocketTimeoutException, TimeoutError):
            pass  # reload dispatched, ack may not come before disconnect
        return True, "reload sent"
    except Exception as e:
        return False, f"send: {e}"
    finally:
        try:
            ws.close()
        except Exception:
            pass


def main():
    target_version = _target_version()
    with AUDIT_PATH.open(encoding="utf-8") as f:
        audit = json.load(f)

    print("# Reload plan\n")
    plan: list[dict] = []
    for plat, tabs in audit.items():
        for tab in tabs:
            need, reason = _decide_reload(tab, target_version)
            url_for_check = tab.get("url") or tab.get("url_snapshot") or ""
            auth_wall = _is_auth_wall(url_for_check)
            if auth_wall:
                need = False
                reason = f"auth-wall URL, skipping ({reason})"
            plan.append({
                "platform": plat,
                "target_id": tab["target_id"],
                "url": url_for_check,
                "ws": tab["ws"],
                "reason": reason,
                "action": "reload" if need else "skip",
                "auth_wall": auth_wall,
                "heap_mb": tab.get("heap_mb"),
                "cs": tab.get("cs"),
                "cs_version": tab.get("cs_version"),
                "responsive_main": tab.get("responsive_main"),
            })

    for p in plan:
        marker = "RELOAD" if p["action"] == "reload" else "skip"
        print(f"  [{marker:6}] {p['platform']:10} {p['target_id'][:12]}  cs={p['cs']}  ver={p['cs_version']}  resp={p['responsive_main']}  heap={p['heap_mb']}  reason={p['reason']}")
        print(f"           url={p['url'][:100]}")
    print()

    print("# Executing reloads sequentially...")
    results = []
    for p in plan:
        if p["action"] != "reload":
            results.append({**p, "status": "skipped"})
            continue
        print(f"  reload {p['platform']:10} {p['target_id'][:12]} ...", end=" ")
        ok, msg = send_reload(p["ws"], p["target_id"])
        print(f"{'OK' if ok else 'FAIL'}: {msg}")
        results.append({**p, "status": "ok" if ok else "fail", "detail": msg})
        # Space out — the box is slow and we don't want to slam it
        time.sleep(1.5)

    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PLAN_PATH.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n# wrote {PLAN_PATH}")
    print(f"# reloaded {sum(1 for r in results if r['status']=='ok')} tab(s), "
          f"failed {sum(1 for r in results if r['status']=='fail')}, "
          f"skipped {sum(1 for r in results if r['status']=='skipped')}")


if __name__ == "__main__":
    main()
