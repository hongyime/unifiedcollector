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
import os
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

import websocket

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CDP_HOST = os.getenv(
    "UC_CHROME_CDP_URL",
    f"http://127.0.0.1:{os.getenv('UC_CHROME_CDP_PORT', '9333')}",
)
REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = REPO_ROOT / "tmp" / "browser_tab_audit_result.json"
PLAN_PATH = REPO_ROOT / "tmp" / "browser_tab_reload_plan.json"
HARD_REOPEN_PLATFORMS = {
    p.strip().lower()
    for p in os.getenv(
        "UC_BROWSER_HARD_REOPEN_PLATFORMS",
        "instagram,threads,tiktok,x,facebook,strava",
    ).split(",")
    if p.strip()
}
HARD_REOPEN_URLS = {
    "tiktok": [
        "https://www.tiktok.com/foryou",
        "https://www.tiktok.com/following",
        "https://www.tiktok.com/explore",
    ],
    "x": [
        "https://x.com/home",
    ],
}
CLOSE_UNHEALTHY_DUPLICATES = os.getenv("UC_BROWSER_CLOSE_UNHEALTHY_DUPLICATES", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}


def _target_version() -> str:
    with (REPO_ROOT / "extension" / "manifest.json").open(encoding="utf-8") as f:
        return str(json.load(f).get("version") or "").strip()


def _list_targets() -> list[dict]:
    resp = urllib.request.urlopen(f"{CDP_HOST}/json/list", timeout=10)
    return json.loads(resp.read().decode("utf-8"))


def _cdp_request(path: str, timeout: float = 8.0, method: str = "GET") -> tuple[bool, str]:
    url = f"{CDP_HOST}{path}"
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return True, body[:500]
    except Exception as exc:
        return False, str(exc)


def _close_target(target_id: str) -> tuple[bool, str]:
    return _cdp_request(f"/json/close/{urllib.parse.quote(str(target_id), safe='')}", timeout=8.0)


def _open_url(url: str) -> tuple[bool, str]:
    return _cdp_request(f"/json/new?{urllib.parse.quote(url, safe='')}", timeout=8.0, method="PUT")


def _decide_reload(tab: dict, target_version: str) -> tuple[bool, str]:
    if tab.get("page_health_status") == "recoverable_error_shell":
        reason = tab.get("page_health_reason") or "recoverable_error_shell"
        return True, f"page health: {reason}"
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
    markers = (
        "/login",
        "/signin",
        "/checkpoint",
        "/challenge",
        "/i/flow/login",
        "/i/jf/onboarding",
        "mode=login",
        "redirect_after_login",
        "auth_platform",
        "recaptcha",
        "/log_out",
        "?logout=",
        "&logout=",
    )
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


def _target_disappeared(message: str) -> bool:
    text = (message or "").lower()
    return "no such target" in text or "target closed" in text or "target detached" in text


def _load_previous_plan() -> list[dict]:
    try:
        with PLAN_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _platform_had_previous_unresponsive_reload(previous: list[dict], platform: str) -> bool:
    for item in previous:
        if str(item.get("platform") or "").lower() != platform:
            continue
        if item.get("action") == "reload" and "unresponsive" in str(item.get("reason") or "").lower():
            return True
    return False


def _previous_reload_for_url(previous: list[dict], platform: str, url: str) -> dict | None:
    normalized_url = str(url or "").split("#", 1)[0]
    for item in previous:
        if str(item.get("platform") or "").lower() != platform:
            continue
        if item.get("action") != "reload":
            continue
        prior_url = str(item.get("url") or "").split("#", 1)[0]
        if prior_url == normalized_url:
            return item
    return None


def _is_stuck_tab_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return (
        "unresponsive" in text
        or "no content script attached" in text
        or "page health:" in text
        or "recoverable_error_shell" in text
    )


def _hard_reopen_platform(platform: str, plans: list[dict]) -> list[dict]:
    results: list[dict] = []
    for p in plans:
        ok, msg = _close_target(p["target_id"])
        results.append({**p, "action": "hard_reopen_close", "status": "ok" if ok else "fail", "detail": msg})
        print(f"  close  {platform:10} {p['target_id'][:12]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.5)
    reopen_urls = HARD_REOPEN_URLS.get(platform) or list(dict.fromkeys(
        str(p.get("url") or "").strip() for p in plans if str(p.get("url") or "").strip()
    ))
    for url in reopen_urls:
        ok, msg = _open_url(url)
        results.append({
            "platform": platform,
            "target_id": None,
            "url": url,
            "ws": None,
            "reason": "reopen canonical tab after repeated unresponsive reload",
            "action": "hard_reopen_open",
            "auth_wall": False,
            "heap_mb": None,
            "cs": None,
            "cs_version": None,
            "responsive_main": None,
            "status": "ok" if ok else "fail",
            "detail": msg,
        })
        print(f"  open   {platform:10} {url[:80]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.8)
    return results


def _hard_reopen_repeated_tabs(platform: str, plans: list[dict]) -> list[dict]:
    """Close only repeated stuck tabs and reopen their current URLs."""
    results: list[dict] = []
    for p in plans:
        ok, msg = _close_target(p["target_id"])
        results.append({**p, "action": "hard_reopen_close", "status": "ok" if ok else "fail", "detail": msg})
        print(f"  close  {platform:10} {p['target_id'][:12]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.5)
    if platform == "x":
        reopen_urls = HARD_REOPEN_URLS["x"]
    else:
        reopen_urls = list(dict.fromkeys(
            str(p.get("url") or "").strip() or HARD_REOPEN_URLS.get(platform, ["about:blank"])[0]
            for p in plans
        ))
    for url in reopen_urls:
        ok, msg = _open_url(url)
        results.append({
            "platform": platform,
            "target_id": None,
            "url": url,
            "ws": None,
            "reason": "reopen repeated stuck tab after prior soft reload",
            "action": "hard_reopen_open",
            "auth_wall": False,
            "heap_mb": None,
            "cs": None,
            "cs_version": None,
            "responsive_main": None,
            "status": "ok" if ok else "fail",
            "detail": msg,
        })
        print(f"  open   {platform:10} {str(url)[:80]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.8)
    return results


def main():
    target_version = _target_version()
    previous_plan = _load_previous_plan()
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

    hard_reopen_platforms: set[str] = set()
    hard_reopen_tabs: set[str] = set()
    healthy_platforms = {
        p["platform"]
        for p in plan
        if p["action"] == "skip"
        and p["auth_wall"] is False
        and p.get("responsive_main") is True
        and p.get("cs") is True
        and (not target_version or p.get("cs_version") == target_version)
    }
    duplicate_close_tabs = {
        str(p["target_id"])
        for p in plan
        if CLOSE_UNHEALTHY_DUPLICATES
        and p["platform"] in healthy_platforms
        and p["action"] == "reload"
        and not p["auth_wall"]
    }
    stale_auth_wall_close_tabs = {
        str(p["target_id"])
        for p in plan
        if CLOSE_UNHEALTHY_DUPLICATES
        and p["platform"] in healthy_platforms
        and p["auth_wall"]
    }
    seen_healthy_urls: set[tuple[str, str]] = set()
    duplicate_healthy_close_tabs: set[str] = set()
    if CLOSE_UNHEALTHY_DUPLICATES:
        for p in plan:
            key = (str(p["platform"]), str(p.get("url") or ""))
            if (
                p["action"] == "skip"
                and p["auth_wall"] is False
                and p.get("responsive_main") is True
                and p.get("cs") is True
                and key[1]
            ):
                if key in seen_healthy_urls:
                    duplicate_healthy_close_tabs.add(str(p["target_id"]))
                else:
                    seen_healthy_urls.add(key)
    for platform in HARD_REOPEN_PLATFORMS:
        platform_plans = [p for p in plan if p["platform"] == platform and not p["auth_wall"]]
        if not platform_plans:
            continue
        if platform in HARD_REOPEN_URLS:
            shell_tabs = [
                p for p in platform_plans
                if p["action"] == "reload" and "page health:" in str(p["reason"]).lower()
            ]
            if shell_tabs:
                hard_reopen_tabs.update(str(p["target_id"]) for p in shell_tabs)
                continue
        unresponsive = [
            p for p in platform_plans
            if p["action"] == "reload" and "unresponsive" in str(p["reason"]).lower()
        ]
        if len(unresponsive) == len(platform_plans) and _platform_had_previous_unresponsive_reload(previous_plan, platform):
            hard_reopen_platforms.add(platform)
            continue
        for p in platform_plans:
            if p["action"] != "reload" or not _is_stuck_tab_reason(str(p.get("reason") or "")):
                continue
            previous = _previous_reload_for_url(previous_plan, platform, p.get("url") or "")
            if previous and _is_stuck_tab_reason(str(previous.get("reason") or "")):
                hard_reopen_tabs.add(str(p["target_id"]))

    for p in plan:
        marker = "RELOAD" if p["action"] == "reload" else "skip"
        print(f"  [{marker:6}] {p['platform']:10} {p['target_id'][:12]}  cs={p['cs']}  ver={p['cs_version']}  resp={p['responsive_main']}  heap={p['heap_mb']}  reason={p['reason']}")
        print(f"           url={p['url'][:100]}")
    print()

    print("# Executing reloads sequentially...")
    results = []
    for platform in sorted(hard_reopen_platforms):
        platform_plans = [p for p in plan if p["platform"] == platform and not p["auth_wall"]]
        if not platform_plans:
            continue
        print(f"  hard reopen {platform}: repeated unresponsive reloads; closing stale tabs and opening canonical tabs")
        results.extend(_hard_reopen_platform(platform, platform_plans))

    for platform in HARD_REOPEN_PLATFORMS:
        if platform in hard_reopen_platforms:
            continue
        platform_plans = [
            p for p in plan
            if p["platform"] == platform and str(p["target_id"]) in hard_reopen_tabs and not p["auth_wall"]
        ]
        if not platform_plans:
            continue
        print(f"  hard reopen {platform}: repeated stuck tab(s); closing only stale tabs and reopening same URL")
        results.extend(_hard_reopen_repeated_tabs(platform, platform_plans))

    for p in plan:
        if str(p["target_id"]) not in duplicate_close_tabs:
            continue
        ok, msg = _close_target(p["target_id"])
        results.append({**p, "action": "close_duplicate_unhealthy", "status": "ok" if ok else "fail", "detail": msg})
        print(f"  close  {p['platform']:10} {p['target_id'][:12]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.5)

    for p in plan:
        if str(p["target_id"]) not in stale_auth_wall_close_tabs:
            continue
        ok, msg = _close_target(p["target_id"])
        results.append({**p, "action": "close_duplicate_auth_wall", "status": "ok" if ok else "fail", "detail": msg})
        print(f"  close  {p['platform']:10} {p['target_id'][:12]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.5)

    for p in plan:
        if str(p["target_id"]) not in duplicate_healthy_close_tabs:
            continue
        ok, msg = _close_target(p["target_id"])
        results.append({**p, "action": "close_duplicate_healthy_url", "status": "ok" if ok else "fail", "detail": msg})
        print(f"  close  {p['platform']:10} {p['target_id'][:12]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.5)

    for p in plan:
        if p["platform"] in hard_reopen_platforms and not p["auth_wall"]:
            continue
        if str(p["target_id"]) in hard_reopen_tabs and not p["auth_wall"]:
            continue
        if str(p["target_id"]) in duplicate_close_tabs:
            continue
        if str(p["target_id"]) in stale_auth_wall_close_tabs:
            continue
        if str(p["target_id"]) in duplicate_healthy_close_tabs:
            continue
        if p["action"] != "reload":
            results.append({**p, "status": "skipped"})
            continue
        print(f"  reload {p['platform']:10} {p['target_id'][:12]} ...", end=" ")
        ok, msg = send_reload(p["ws"], p["target_id"])
        if not ok and _target_disappeared(msg):
            print(f"SKIP: target disappeared before reload ({msg})")
            results.append({**p, "status": "skipped", "detail": msg, "skip_reason": "target_disappeared"})
        else:
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
