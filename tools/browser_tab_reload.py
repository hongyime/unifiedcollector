#!/usr/bin/env python3
"""
Soft-reload a set of Chrome tabs via CDP Page.reload.

Reads tmp/browser_tab_audit_result.json, decides which tabs need reload, and issues
Page.reload on each with ignoreCache=false (soft reload). Tabs already healthy
(cs=True cs_version=<extension/manifest.json version>) are skipped.

By default every browser-managed platform is eligible for maintenance. Set
UC_BROWSER_EXCLUDED_PLATFORMS to temporarily quarantine a fragile platform.
"""

from __future__ import annotations

import json
import os
import argparse
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

import websocket

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CDP_HOST = os.getenv(
    "UC_CHROME_CDP_URL",
    f"http://127.0.0.1:{os.getenv('UC_CHROME_CDP_PORT', '9336')}",
)
REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = REPO_ROOT / "tmp" / "browser_tab_audit_result.json"
PLAN_PATH = REPO_ROOT / "tmp" / "browser_tab_reload_plan.json"
HARD_REOPEN_PLATFORMS = {
    p.strip().lower()
    for p in os.getenv(
        "UC_BROWSER_HARD_REOPEN_PLATFORMS",
        "instagram,threads,tiktok,lemon8,facebook,strava,x",
    ).split(",")
    if p.strip()
}
EXCLUDED_AUTO_PLATFORMS = {
    p.strip().lower()
    for p in os.getenv("UC_BROWSER_EXCLUDED_PLATFORMS", "").split(",")
    if p.strip()
}
CLOSE_EXCLUDED_AUTO_PLATFORMS = os.getenv("UC_BROWSER_CLOSE_EXCLUDED_PLATFORM_TABS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
EXPANDED_PLATFORM_TABS = os.getenv("UC_CHROME_OPEN_EXPANDED_PLATFORM_TABS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
} or os.getenv("UC_BROWSER_EXPANDED_PLATFORM_TABS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
_TIKTOK_HARD_REOPEN_URLS = ["https://www.tiktok.com/following"]
if EXPANDED_PLATFORM_TABS:
    _TIKTOK_HARD_REOPEN_URLS.extend(
        [
            "https://www.tiktok.com/foryou",
            "https://www.tiktok.com/explore",
        ]
    )
HARD_REOPEN_URLS = {
    "instagram": [
        "https://www.instagram.com/explore/",
    ],
    "threads": [
        "https://www.threads.com/following",
    ],
    "tiktok": _TIKTOK_HARD_REOPEN_URLS,
    "lemon8": [
        "https://www.lemon8-app.com/topic/singapore?region=sg",
    ],
    "x": [
        "https://x.com/home",
    ],
    "facebook": [
        "https://www.facebook.com/",
    ],
    "strava": [
        "https://www.strava.com/dashboard",
    ],
}
PLATFORM_ALIAS_HOSTS = {
    "x": {"twitter.com", "www.twitter.com"},
}
CLOSE_UNHEALTHY_DUPLICATES = os.getenv("UC_BROWSER_CLOSE_UNHEALTHY_DUPLICATES", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
DASHBOARD_HEALTH_URL = os.getenv(
    "UC_DASHBOARD_HEALTH_URL",
    "http://127.0.0.1:8001/health?include_sources=true",
)
DASHBOARD_HEALTH_TIMEOUT_SECONDS = float(os.getenv("UC_DASHBOARD_HEALTH_TIMEOUT_SECONDS", "20"))


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


def _stale_browser_issue_platforms(platform_filter: set[str] | None = None) -> set[str]:
    """Return browser platforms currently degraded by stale content.

    Tab-local health can be green while the extension is no longer producing
    posts/media events. The dashboard source matrix is the canonical liveness
    view, so feed its stale-browser issues back into maintenance.
    """
    if not DASHBOARD_HEALTH_URL:
        return set()
    try:
        with urllib.request.urlopen(DASHBOARD_HEALTH_URL, timeout=DASHBOARD_HEALTH_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        print(f"  [WARN  ] dashboard stale-source check skipped: {exc}")
        return set()
    stale: set[str] = set()
    rows = list(payload.get("source_issues") or [])
    rows.extend(payload.get("sources") or [])
    for issue in rows:
        if not isinstance(issue, dict):
            continue
        source = str(issue.get("source") or issue.get("platform") or "").lower()
        if source not in HARD_REOPEN_PLATFORMS or source in EXCLUDED_AUTO_PLATFORMS:
            continue
        if platform_filter is not None and source not in platform_filter:
            continue
        detail = " ".join(
            str(issue.get(key) or "")
            for key in ("kind", "detail", "source_health_error", "browser_health_reason", "status_label")
        ).lower()
        if issue.get("browser_content_stale") is True or "browser content progress is" in detail:
            stale.add(source)
    return stale


def _reload_cooldown_seconds() -> float:
    try:
        minutes = float(os.getenv("UC_TAB_RELOAD_429_COOLDOWN_MINUTES", "75"))
    except (TypeError, ValueError):
        minutes = 75.0
    return max(0.0, minutes * 60.0)


def _within_reload_cooldown(previous_reloads: list[dict], platform: str, now: float | None = None) -> bool:
    """True while a prior same-platform shell reload is inside its cooldown window."""
    cooldown = _reload_cooldown_seconds()
    if cooldown <= 0:
        return False
    now = time.time() if now is None else now
    latest = 0.0
    for entry in previous_reloads:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("platform") or "").lower() != platform:
            continue
        text = str(entry.get("reason") or "")
        if "http_429" not in text and "non-canonical" not in text:
            continue
        try:
            ts = float(entry.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        latest = max(latest, ts)
    if latest <= 0:
        return False
    return (now - latest) < cooldown


def _consecutive_shell_cycles(previous_plan: list[dict], platform: str) -> int:
    """Count trailing same-platform reload cycles whose reason was a page shell."""
    count = 0
    for entry in reversed(previous_plan or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("platform") or "").lower() != platform:
            continue
        if str(entry.get("action") or "") != "reload":
            continue
        if _is_stuck_tab_reason(str(entry.get("reason") or "")):
            count += 1
        else:
            break
    return count


def _decide_reload(
    tab: dict,
    target_version: str,
    stale_platforms: set[str] | None = None,
    previous_reloads: list[dict] | None = None,
) -> tuple[bool, str]:
    platform = str(tab.get("platform") or "").lower()
    url = str(tab.get("url") or tab.get("url_snapshot") or "")
    if platform in EXCLUDED_AUTO_PLATFORMS:
        return False, f"{platform} excluded from automatic tab reload"
    if (
        platform == "instagram"
        and str(tab.get("page_health_reason") or "").lower() == "http_429"
        and _within_reload_cooldown(previous_reloads or [], "instagram")
    ):
        # Reopening into an active 429 wall just churns the tab and deepens the
        # rate-limit; wait out the cooldown before touching it again.
        return False, "instagram http_429 reload cooldown active"
    if platform == "x" and not _is_canonical_x_recovery_url(url):
        return True, "x non-canonical recovery URL"
    if platform != "x" and platform in HARD_REOPEN_URLS and not _is_canonical_platform_url(platform, url):
        return True, f"{platform} non-canonical platform URL"
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
    if platform in (stale_platforms or set()):
        return True, "source health: stale browser content"
    return False, "healthy"


def _is_canonical_x_recovery_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except Exception:
        return False
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {"x.com", "www.x.com"}:
        return False
    query_text = (parsed.query or "").lower()
    if "failedscript" in query_text:
        # failedScript marks X's own crash-recovery shell, not a usable feed.
        # parse_qs drops blank values, so inspect the raw query string.
        return False
    path = parsed.path.rstrip("/") or "/"
    return path in {"/home", "/explore"}


def _hosts_for_platform(platform: str) -> set[str]:
    hosts: set[str] = set(PLATFORM_ALIAS_HOSTS.get(platform, set()))
    for candidate in HARD_REOPEN_URLS.get(platform, []):
        try:
            host = urllib.parse.urlparse(candidate).netloc.lower().split(":", 1)[0]
        except Exception:
            host = ""
        if host:
            hosts.add(host)
    return hosts


def _excluded_platform_for_url(url: str) -> str | None:
    try:
        host = urllib.parse.urlparse(str(url or "")).netloc.lower().split(":", 1)[0]
    except Exception:
        return None
    if not host:
        return None
    for platform in EXCLUDED_AUTO_PLATFORMS:
        if host in _hosts_for_platform(platform):
            return platform
    return None


def _is_canonical_platform_url(platform: str, url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except Exception:
        return False
    allowed_queries = {
        urllib.parse.urlparse(candidate).query
        for candidate in HARD_REOPEN_URLS.get(platform, [])
    }
    if parsed.query and parsed.query not in allowed_queries:
        return False
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path.rstrip("/") or "/"
    for candidate in HARD_REOPEN_URLS.get(platform, []):
        wanted = urllib.parse.urlparse(candidate)
        wanted_host = wanted.netloc.lower().split(":", 1)[0]
        wanted_path = wanted.path.rstrip("/") or "/"
        if host == wanted_host and path == wanted_path:
            return True
    return False


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
    return (
        "no such target" in text
        or "target closed" in text
        or "target detached" in text
        or "http error 404" in text
        or "404: not found" in text
    )


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
        or "non-canonical recovery url" in text
        or "non-canonical platform url" in text
        or "stale browser content" in text
    )


def _append_live_excluded_target_closures(plan: list[dict], platform_filter: set[str] | None = None) -> None:
    if not CLOSE_EXCLUDED_AUTO_PLATFORMS or not EXCLUDED_AUTO_PLATFORMS:
        return
    planned_ids = {
        str(p.get("target_id"))
        for p in plan
        if p.get("target_id") is not None
    }
    try:
        targets = _list_targets()
    except Exception as exc:
        print(f"  [WARN  ] live excluded tab sweep skipped: {exc}")
        return
    for target in targets:
        if target.get("type") != "page":
            continue
        target_id = str(target.get("id") or "")
        if not target_id or target_id in planned_ids:
            continue
        url = str(target.get("url") or "")
        platform = _excluded_platform_for_url(url)
        if not platform or _is_auth_wall(url):
            continue
        if platform_filter is not None and platform not in platform_filter:
            continue
        plan.append({
            "platform": platform,
            "target_id": target_id,
            "url": url,
            "ws": target.get("webSocketDebuggerUrl"),
            "reason": f"{platform} live CDP target excluded from automatic browser maintenance",
            "action": "close_excluded",
            "auth_wall": False,
            "heap_mb": None,
            "cs": None,
            "cs_version": None,
            "responsive_main": None,
        })


def _append_missing_stale_platform_opens(
    plan: list[dict],
    stale_platforms: set[str],
    platform_filter: set[str] | None = None,
) -> None:
    """Open a canonical tab when source liveness says a browser platform is stale.

    The normal reload path only sees tabs present in the audit file. If a
    managed platform has no tab at all, stale browser-content issues would stay
    open until a full Chrome restart. Opening the canonical tab is cheaper and
    lets the extension resume capture in place.
    """
    planned_platforms = {str(item.get("platform") or "").lower() for item in plan}
    for platform in sorted(stale_platforms):
        if platform_filter is not None and platform not in platform_filter:
            continue
        if platform in planned_platforms or platform in EXCLUDED_AUTO_PLATFORMS:
            continue
        urls = HARD_REOPEN_URLS.get(platform) or []
        if not urls:
            continue
        plan.append({
            "platform": platform,
            "target_id": "",
            "url": urls[0],
            "ws": None,
            "reason": "source health: stale browser content and no tab open",
            "action": "open_missing",
            "auth_wall": False,
            "heap_mb": None,
            "cs": None,
            "cs_version": None,
            "responsive_main": None,
        })


def _append_duplicate_control_tab_closures(plan: list[dict]) -> None:
    planned_ids = {
        str(p.get("target_id"))
        for p in plan
        if p.get("target_id") is not None
    }
    try:
        targets = _list_targets()
    except Exception as exc:
        print(f"  [WARN  ] live extension control tab sweep skipped: {exc}")
        return
    primary_id = os.getenv("UC_EXTENSION_ID", "pkmdmcklnjdeocoeigmlakhomhhcpafb")
    def is_control_tab(target: dict) -> bool:
        if target.get("type") != "page":
            return False
        try:
            parsed = urllib.parse.urlparse(str(target.get("url") or ""))
        except Exception:
            return False
        return parsed.scheme == "chrome-extension" and parsed.path == "/tabs.html"

    controls = [target for target in targets if is_control_tab(target)]
    if len(controls) <= 1:
        return
    controls.sort(key=lambda target: (
        0 if str(target.get("url") or "") == f"chrome-extension://{primary_id}/tabs.html" else 1,
        1 if str(target.get("url") or "").startswith(f"chrome-extension://{primary_id}/tabs.html") else 2,
        str(target.get("id") or ""),
    ))
    keep_id = str(controls[0].get("id") or "")
    for target in controls[1:]:
        target_id = str(target.get("id") or "")
        if not target_id or target_id in planned_ids:
            continue
        plan.append({
            "platform": "extension",
            "target_id": target_id,
            "url": str(target.get("url") or ""),
            "ws": target.get("webSocketDebuggerUrl"),
            "reason": f"duplicate extension control tab; keeping {keep_id[:12]}",
            "action": "close_duplicate_control_tab",
            "auth_wall": False,
            "heap_mb": None,
            "cs": None,
            "cs_version": None,
            "responsive_main": None,
        })


def _hard_reopen_platform(platform: str, plans: list[dict]) -> list[dict]:
    results: list[dict] = []
    for p in plans:
        ok, msg = _close_target(p["target_id"])
        if not ok and _target_disappeared(msg):
            ok = True
            msg = f"target already disappeared before close ({msg})"
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
        if not ok and _target_disappeared(msg):
            ok = True
            msg = f"target already disappeared before close ({msg})"
        results.append({**p, "action": "hard_reopen_close", "status": "ok" if ok else "fail", "detail": msg})
        print(f"  close  {platform:10} {p['target_id'][:12]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.5)
    reopen_urls = HARD_REOPEN_URLS.get(platform)
    if not reopen_urls:
        reopen_urls = list(dict.fromkeys(
            str(p.get("url") or "").strip() or "about:blank"
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


def _parse_platform_filter(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    platforms = {
        item.strip().lower()
        for item in str(raw).split(",")
        if item.strip()
    }
    unknown = platforms - set(HARD_REOPEN_URLS)
    if unknown:
        raise SystemExit(f"unknown --platforms value(s): {', '.join(sorted(unknown))}")
    return platforms


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reload unhealthy Collector browser tabs via CDP.")
    parser.add_argument(
        "--platforms",
        help="Comma-separated platform filter. When set, only those platform tabs are repaired.",
    )
    parser.add_argument(
        "--hard-reopen",
        action="store_true",
        help="Compatibility flag; hard reopen decisions are still made from audit health.",
    )
    parser.add_argument("--json", action="store_true", help="Compatibility flag; output remains text plus plan JSON file.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    args = _parse_args(argv)
    platform_filter = _parse_platform_filter(args.platforms)
    target_version = _target_version()
    previous_plan = _load_previous_plan()
    stale_platforms = _stale_browser_issue_platforms(platform_filter)
    if stale_platforms:
        print("# Dashboard stale browser sources: " + ", ".join(sorted(stale_platforms)))
    with AUDIT_PATH.open(encoding="utf-8") as f:
        audit = json.load(f)

    print("# Reload plan\n")
    plan: list[dict] = []
    for plat, tabs in audit.items():
        if str(plat).startswith("_") or not isinstance(tabs, list):
            continue
        platform = str(plat).lower()
        if platform_filter is not None and platform not in platform_filter:
            continue
        for tab in tabs:
            previous_reload_entries = [
                e for e in previous_plan
                if isinstance(e, dict) and e.get("action") == "reload"
            ]
            need, reason = _decide_reload(
                tab,
                target_version,
                stale_platforms,
                previous_reloads=previous_reload_entries,
            )
            url_for_check = tab.get("url") or tab.get("url_snapshot") or ""
            auth_wall = _is_auth_wall(url_for_check)
            action = "reload" if need else "skip"
            if (
                CLOSE_EXCLUDED_AUTO_PLATFORMS
                and str(plat).lower() in EXCLUDED_AUTO_PLATFORMS
                and not auth_wall
            ):
                action = "close_excluded"
                reason = f"{plat} excluded from automatic browser maintenance"
            if auth_wall:
                need = False
                action = "skip"
                reason = f"auth-wall URL, skipping ({reason})"
            plan.append({
                "platform": plat,
                "target_id": tab["target_id"],
                "url": url_for_check,
                "ws": tab["ws"],
                "reason": reason,
                "action": action,
                "auth_wall": auth_wall,
                "heap_mb": tab.get("heap_mb"),
                "cs": tab.get("cs"),
                "cs_version": tab.get("cs_version"),
                "responsive_main": tab.get("responsive_main"),
            })
            if platform == "x" and action == "reload" and "non-canonical" in reason:
                cycles = _consecutive_shell_cycles(previous_plan, "x")
                if cycles >= 2:
                    plan[-1]["escalation"] = {
                        "level": cycles + 1,
                        "message": (
                            f"x page-shell churn across {cycles} prior cycles - "
                            "verify X session/auth manually"
                        ),
                    }
                    print(
                        f"  ESCALATION: x repeated page-shell ({cycles} cycles) - manual auth check recommended"
                    )
    _append_live_excluded_target_closures(plan, platform_filter)
    _append_missing_stale_platform_opens(plan, stale_platforms, platform_filter)
    _append_duplicate_control_tab_closures(plan)

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
                if p["action"] == "reload"
                and (
                    "page health:" in str(p["reason"]).lower()
                    or "non-canonical recovery url" in str(p["reason"]).lower()
                    or "non-canonical platform url" in str(p["reason"]).lower()
                )
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
        if p["action"] == "close_excluded":
            continue
        marker = "RELOAD" if p["action"] == "reload" else "skip"
        print(f"  [{marker:6}] {p['platform']:10} {p['target_id'][:12]}  cs={p['cs']}  ver={p['cs_version']}  resp={p['responsive_main']}  heap={p['heap_mb']}  reason={p['reason']}")
        print(f"           url={p['url'][:100]}")
    print()

    print("# Executing reloads sequentially...")
    results = []
    for p in plan:
        if p["action"] != "open_missing":
            continue
        ok, msg = _open_url(str(p["url"]))
        results.append({**p, "status": "ok" if ok else "fail", "detail": msg})
        print(f"  open   {p['platform']:10} {str(p['url'])[:80]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(1.0)

    for p in plan:
        if p["action"] not in {"close_excluded", "close_duplicate_control_tab"}:
            continue
        ok, msg = _close_target(p["target_id"])
        results.append({**p, "status": "ok" if ok else "fail", "detail": msg})
        print(f"  close  {p['platform']:10} {p['target_id'][:12]} ... {'OK' if ok else 'FAIL'}: {msg[:160]}")
        time.sleep(0.5)

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
        if p["action"] in {"close_excluded", "close_duplicate_control_tab", "open_missing"}:
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
    # Stamp wall-clock ts on every result so the next cycle can apply
    # cooldown windows (e.g. instagram http_429) without extra state.
    stamped_results = [{**r, "ts": r.get("ts") or time.time()} for r in results]
    with PLAN_PATH.open("w", encoding="utf-8") as f:
        json.dump(stamped_results, f, indent=2, default=str)
    print(f"\n# wrote {PLAN_PATH}")
    print(f"# reloaded {sum(1 for r in results if r['status']=='ok')} tab(s), "
          f"failed {sum(1 for r in results if r['status']=='fail')}, "
          f"skipped {sum(1 for r in results if r['status']=='skipped')}")


if __name__ == "__main__":
    main()
