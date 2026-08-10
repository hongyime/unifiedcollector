#!/usr/bin/env python3
"""
UnifiedCollector Chrome-tab health audit v2.

Key fix vs v1: content.js runs in the isolated world (manifest default), so its
globals are NOT visible in the default Runtime.evaluate on the page's main-world
context. We need to enumerate execution contexts, find the isolated world for
extension pkmdmcklnjdeocoeigmlakhomhhcpafb, and evaluate there.

For each candidate tab we do:
  1. Fresh CDP WS connect
  2. Enable Runtime (also emits Runtime.executionContextCreated for every existing world)
  3. Runtime.evaluate main-world (heap/dom/url) with 8s timeout
  4. Find isolated world for our ext -> Runtime.evaluate there for cs / install_id
  5. Performance.getMetrics as ground-truth
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import websocket

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CDP_HOST = os.getenv(
    "UC_CHROME_CDP_URL",
    f"http://127.0.0.1:{os.getenv('UC_CHROME_CDP_PORT', '9333')}",
)
EXT_ID = os.getenv("UC_EXTENSION_ID", "pkmdmcklnjdeocoeigmlakhomhhcpafb")
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "tmp" / "browser_tab_audit_result.json"


def _float_env(name: str, default: float) -> float:
    raw = ""
    try:
        import os
        raw = os.getenv(name, "")
    except Exception:
        raw = ""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(0.5, value)


CONNECT_TIMEOUT = _float_env("UC_TAB_AUDIT_CONNECT_TIMEOUT_SECONDS", 2.0)
RUNTIME_ENABLE_TIMEOUT = _float_env("UC_TAB_AUDIT_RUNTIME_ENABLE_TIMEOUT_SECONDS", 4.0)
MAIN_TIMEOUT = _float_env("UC_TAB_AUDIT_MAIN_TIMEOUT_SECONDS", 8.0)
ISO_TIMEOUT = _float_env("UC_TAB_AUDIT_ISO_TIMEOUT_SECONDS", 2.0)
PERF_TIMEOUT = _float_env("UC_TAB_AUDIT_PERF_TIMEOUT_SECONDS", 0.8)
DRAIN_SECONDS = _float_env("UC_TAB_AUDIT_CONTEXT_DRAIN_SECONDS", 0.1)
TAB_PAUSE_SECONDS = _float_env("UC_TAB_AUDIT_TAB_PAUSE_SECONDS", 0.1)
ACTIVATE_BEFORE_AUDIT = os.getenv("UC_TAB_AUDIT_ACTIVATE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}

PLATFORMS = {
    "instagram": ["instagram.com"],
    "threads": ["threads.com", "threads.net"],
    "tiktok": ["tiktok.com"],
    "lemon8": ["lemon8-app.com"],
    "x": ["x.com", "twitter.com"],
    "facebook": ["facebook.com"],
    "strava": ["strava.com"],
}
FBSBX = "fbsbx.com"  # facebook iframe, not a real facebook page


def _list_targets() -> list[dict]:
    resp = urllib.request.urlopen(f"{CDP_HOST}/json/list", timeout=8)
    return json.loads(resp.read().decode("utf-8"))


def _classify(url: str) -> str | None:
    if not url or not url.startswith("http"):
        return None
    if FBSBX in url:
        return None
    for plat, domains in PLATFORMS.items():
        for d in domains:
            if f"//{d}" in url or f".{d}" in url:
                return plat
    return None


class CDP:
    def __init__(self, ws_url: str, timeout: float = 8.0):
        self.ws = websocket.create_connection(ws_url, timeout=timeout)
        self.msg_id = 0
        self.events: list[dict] = []

    def _drain(self, deadline: float) -> None:
        """Read any pending events until socket blocks or deadline."""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                self.ws.settimeout(min(0.2, remaining))
                raw = self.ws.recv()
                m = json.loads(raw)
                if "method" in m:
                    self.events.append(m)
                elif "id" in m:
                    # stray response — shouldn't happen; keep it
                    self.events.append(m)
            except (websocket.WebSocketTimeoutException, TimeoutError):
                return

    def send(self, method: str, params: dict | None = None, timeout: float = 8.0) -> dict:
        self.msg_id += 1
        payload = {"id": self.msg_id, "method": method}
        if params:
            payload["params"] = params
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise websocket.WebSocketTimeoutException(f"CDP send({method}) exceeded {timeout}s")
            self.ws.settimeout(remaining)
            raw = self.ws.recv()
            m = json.loads(raw)
            if m.get("id") == self.msg_id:
                return m
            if "method" in m:
                self.events.append(m)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


CONTENT_EVAL_JS = (
    "JSON.stringify({"
    "cs:!!globalThis.__UC_CONTENT_SCRIPT_ACTIVE__,"
    "install:(typeof UC_CONTENT_INSTALL_ID!=='undefined')?UC_CONTENT_INSTALL_ID:null,"
    "cs_version:(globalThis.__UC_CONTENT_SCRIPT_ACTIVE__&&globalThis.__UC_CONTENT_SCRIPT_ACTIVE__.version)||null,"
    "cs_installed_at:(globalThis.__UC_CONTENT_SCRIPT_ACTIVE__&&globalThis.__UC_CONTENT_SCRIPT_ACTIVE__.installed_at)||null,"
    "cs_running:(globalThis.__UC_CONTENT_SCRIPT_ACTIVE__&&globalThis.__UC_CONTENT_SCRIPT_ACTIVE__.running)||null,"
    "url:location.href"
    "})"
)

MAIN_EVAL_JS = (
    "JSON.stringify((()=>{"
    "const counts={articles:document.querySelectorAll('article').length,"
    "videos:document.querySelectorAll('video').length,"
    "images:document.querySelectorAll('img[src],img[srcset]').length,"
    "links:document.querySelectorAll('a[href]').length};"
    "const focused=[...document.querySelectorAll('[role=\"alert\"],[data-e2e*=\"error\" i],[data-e2e*=\"empty\" i],h1,h2,button')]"
    ".map(e=>(e.innerText||e.textContent||'').trim()).filter(Boolean).slice(0,80).join('\\n');"
    "const body=(document.body&&document.body.innerText?document.body.innerText.slice(0,9000):'');"
    "const text=[document.title||'',focused,body].filter(Boolean).join('\\n');"
    "const compact=text.replace(/\\s+/g,' ').trim().slice(0,260);"
    "const low=(counts.articles+counts.videos+counts.images)<4&&counts.links<40;"
    "let health_status='ok',health_reason='';"
    "if(location.href.includes('/?logout=')||(low&&document.querySelector('iframe[src*=\"recaptcha\"]'))){health_status='recoverable_error_shell';health_reason='auth_challenge';}"
    "else if(/sorry,?\\s*we\\s+couldn(?:'|\\u2019)?t\\s+show\\s+that\\s+page/i.test(text)){health_status='recoverable_error_shell';health_reason='sorry_could_not_show_page';}"
    "else if(/couldn(?:'|\\u2019)?t\\s+show\\s+(?:this|that)\\s+page/i.test(text)){health_status='recoverable_error_shell';health_reason='could_not_show_page';}"
    "else if(/this\\s+page\\s+isn(?:'|\\u2019)?t\\s+available|page\\s+not\\s+found/i.test(text)){health_status='recoverable_error_shell';health_reason='page_not_available';}"
    "else if(low&&/\\btry\\s+again\\b/i.test(text)){health_status='recoverable_error_shell';health_reason='try_again_empty_state';}"
    "else if(low&&/something\\s+went\\s+wrong/i.test(text)){health_status='recoverable_error_shell';health_reason='something_went_wrong';}"
    "else if(/sign\\s+in\\s+to\\s+x|log\\s+in\\s+to\\s+x|log\\s+in\\s+to\\s+instagram|log\\s+in\\s+to\\s+tiktok/i.test(text)){health_status='recoverable_error_shell';health_reason='login_wall_text';}"
    "return {url:location.href,nodes:document.querySelectorAll('*').length,"
    "heap_mb:Math.round((performance.memory?performance.memory.usedJSHeapSize:0)/1024/1024),"
    "heap_bytes:performance.memory?performance.memory.usedJSHeapSize:0,docReady:document.readyState,hidden:document.hidden,"
    "page_health_status:health_status,page_health_reason:health_reason,page_health_sample:compact,"
    "page_content_counts:counts};"
    "})())"
)


def audit_tab(target: dict, main_timeout=MAIN_TIMEOUT, iso_timeout=ISO_TIMEOUT) -> dict:
    plat = _classify(target.get("url", ""))
    out = {
        "platform": plat,
        "target_id": target.get("id"),
        "url_snapshot": target.get("url"),
        "title": target.get("title"),
        "ws": target.get("webSocketDebuggerUrl"),
        "responsive_main": None,
        "elapsed_s_main": None,
        "elapsed_s_iso": None,
        "url": None,
        "nodes": None,
        "heap_mb": None,
        "js_heap_used_bytes": None,
        "doc_ready": None,
        "hidden": None,
        "page_health_status": None,
        "page_health_reason": None,
        "page_health_sample": None,
        "page_content_counts": None,
        "cs": None,
        "cs_version": None,
        "cs_running": None,
        "install_id": None,
        "cs_installed_at": None,
        "iso_context_found": False,
        "isolated_worlds": [],
        "error": None,
        "perf_heap_mb": None,
        "perf_nodes": None,
    }
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        out["error"] = "no webSocketDebuggerUrl"
        return out
    if ACTIVATE_BEFORE_AUDIT and target.get("id"):
        try:
            urllib.request.urlopen(f"{CDP_HOST}/json/activate/{target['id']}", timeout=3).read()
            time.sleep(0.8)
        except Exception as e:
            out["error"] = f"activate failed: {e}"

    try:
        cdp = CDP(ws_url, timeout=CONNECT_TIMEOUT)
    except Exception as e:
        out["error"] = f"connect failed: {e}"
        return out

    try:
        # Enable Runtime -> will emit executionContextCreated for every existing world
        try:
            cdp.send("Runtime.enable", timeout=RUNTIME_ENABLE_TIMEOUT)
        except Exception as e:
            out["error"] = f"Runtime.enable failed: {e}"

        # Drain queued events for ~0.5s to collect context descriptors
        cdp._drain(time.time() + DRAIN_SECONDS)

        # Also enable Performance for accurate metrics
        try:
            cdp.send("Performance.enable", timeout=PERF_TIMEOUT)
        except Exception as e:
            out["_perf_enable_err"] = str(e)

        # Try main-world eval
        t0 = time.time()
        try:
            r = cdp.send(
                "Runtime.evaluate",
                {"expression": MAIN_EVAL_JS, "returnByValue": True, "awaitPromise": False},
                timeout=main_timeout,
            )
            out["elapsed_s_main"] = round(time.time() - t0, 3)
            out["responsive_main"] = out["elapsed_s_main"] < main_timeout
            val = (r.get("result") or {}).get("result", {}).get("value")
            if isinstance(val, str):
                p = json.loads(val)
                out["url"] = p.get("url")
                out["nodes"] = p.get("nodes")
                out["heap_mb"] = p.get("heap_mb")
                out["js_heap_used_bytes"] = p.get("heap_bytes")
                out["doc_ready"] = p.get("docReady")
                out["hidden"] = p.get("hidden")
                out["page_health_status"] = p.get("page_health_status")
                out["page_health_reason"] = p.get("page_health_reason")
                out["page_health_sample"] = p.get("page_health_sample")
                out["page_content_counts"] = p.get("page_content_counts")
            else:
                exc = (r.get("result") or {}).get("exceptionDetails")
                if exc:
                    out["error"] = f"main eval exc: {exc.get('text','?')}"
        except (websocket.WebSocketTimeoutException, TimeoutError):
            out["responsive_main"] = False
            out["elapsed_s_main"] = round(time.time() - t0, 3)
            out["error"] = "main Runtime.evaluate timeout"

        # Now find isolated world contexts
        iso_ctx = None
        for ev in cdp.events:
            if ev.get("method") != "Runtime.executionContextCreated":
                continue
            ctx = ev.get("params", {}).get("context", {})
            aux = ctx.get("auxData") or {}
            entry = {
                "id": ctx.get("id"),
                "name": ctx.get("name"),
                "origin": ctx.get("origin"),
                "auxData": aux,
            }
            out["isolated_worlds"].append(entry)
            # Heuristic: extension isolated world usually has origin starting with
            # chrome-extension://<id>/ or the ctx name equals extension name.
            origin = ctx.get("origin") or ""
            name = ctx.get("name") or ""
            is_extension_world = origin.startswith("chrome-extension://")
            if EXT_ID in origin or "UnifiedCollector" in name or is_extension_world or aux.get("type") == "isolated":
                if iso_ctx is None:
                    iso_ctx = ctx.get("id")

        if iso_ctx is not None:
            out["iso_context_found"] = True
            t1 = time.time()
            try:
                r = cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": CONTENT_EVAL_JS,
                        "returnByValue": True,
                        "awaitPromise": False,
                        "contextId": iso_ctx,
                    },
                    timeout=iso_timeout,
                )
                out["elapsed_s_iso"] = round(time.time() - t1, 3)
                val = (r.get("result") or {}).get("result", {}).get("value")
                if isinstance(val, str):
                    p = json.loads(val)
                    out["cs"] = p.get("cs")
                    out["cs_version"] = p.get("cs_version")
                    out["cs_running"] = p.get("cs_running")
                    out["install_id"] = p.get("install")
                    out["cs_installed_at"] = p.get("cs_installed_at")
                    if p.get("url"):
                        out["url"] = out["url"] or p.get("url")
                else:
                    exc = (r.get("result") or {}).get("exceptionDetails")
                    if exc:
                        out["error"] = (out["error"] or "") + f" | iso eval exc: {exc.get('text','?')}"
            except (websocket.WebSocketTimeoutException, TimeoutError):
                out["elapsed_s_iso"] = round(time.time() - t1, 3)
                out["error"] = (out["error"] or "") + " | iso eval timeout"

        # Perf metrics
        try:
            pm = cdp.send("Performance.getMetrics", timeout=PERF_TIMEOUT)
            metrics = {m["name"]: m["value"] for m in (pm.get("result") or {}).get("metrics", [])}
            if metrics.get("JSHeapUsedSize"):
                out["perf_heap_mb"] = round(metrics["JSHeapUsedSize"] / 1024 / 1024)
            out["perf_nodes"] = metrics.get("Nodes")
            if out["heap_mb"] is None and out["perf_heap_mb"]:
                out["heap_mb"] = out["perf_heap_mb"]
            if out["nodes"] is None and out["perf_nodes"]:
                out["nodes"] = out["perf_nodes"]
        except Exception as e:
            out["_perf_err"] = str(e)
    finally:
        cdp.close()

    return out


def main():
    targets = _list_targets()
    pages = [t for t in targets if t.get("type") == "page"]
    print(f"# {len(pages)} page targets total\n")

    grouped: dict[str, list[dict]] = {p: [] for p in PLATFORMS}
    others: list[dict] = []
    for tgt in pages:
        plat = _classify(tgt.get("url", ""))
        if plat:
            grouped[plat].append(tgt)
        else:
            others.append(tgt)

    print("Non-platform pages (skipped):")
    for o in others:
        print(f"  - {o.get('url','')[:100]}  ({o.get('id','')[:12]})")
    print()

    results: dict[str, list[dict]] = {}
    for plat in PLATFORMS:
        tabs = grouped.get(plat, [])
        results[plat] = []
        if not tabs:
            print(f"[{plat}] MISSING — no tab open")
            continue
        for tgt in tabs:
            print(f"[{plat}] auditing {tgt.get('id','')[:12]}  {tgt.get('url','')[:80]}")
            info = audit_tab(tgt)
            results[plat].append(info)
            print(
                f"    -> main.responsive={info['responsive_main']}  main.elapsed={info['elapsed_s_main']}s  "
                f"nodes={info['nodes']}  heap={info['heap_mb']}MB  ready={info['doc_ready']}  hidden={info['hidden']}"
            )
            print(
                f"       iso_ctx={info['iso_context_found']}  iso.elapsed={info['elapsed_s_iso']}s  "
                f"cs={info['cs']}  cs_version={info['cs_version']}  cs_running={info['cs_running']}"
            )
            if info["error"]:
                print(f"       ERROR: {info['error']}")
            if info.get("page_health_status") and info["page_health_status"] != "ok":
                print(
                    f"       PAGE_HEALTH: {info['page_health_status']}  "
                    f"reason={info.get('page_health_reason')}  sample={info.get('page_health_sample')}"
                )
            time.sleep(TAB_PAUSE_SECONDS)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n# wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
