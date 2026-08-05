"""Time a chrome.runtime.sendMessage log from a content-script tab."""
import io
import json
import sys
import time
import urllib.request

import websocket

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def rpc(ws, request_id, method, params=None, timeout=8):
    body = {"id": request_id, "method": method}
    if params:
        body["params"] = params
    ws.settimeout(timeout)
    ws.send(json.dumps(body))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == request_id:
            return r


def main():
    ts = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/list").read())
    # Pick a scraper page (not tabs.html)
    candidates = [t for t in ts if t.get("type") == "page" and (
        "instagram.com" in t.get("url", "") or "x.com" in t.get("url", "") or
        "tiktok.com" in t.get("url", "") or "threads.com" in t.get("url", ""))]
    if not candidates:
        print("no scraper tab found")
        return
    t = candidates[0]
    print(f"target: {t.get('url','')[:80]}")
    ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=30, origin="http://127.0.0.1:9222")
    try:
        rpc(ws, 1, "Runtime.enable")
        # Get the isolated world (content script) execution contexts:
        # simpler — inject via page main world and try direct chrome.runtime access.
        # In MAIN world, chrome.runtime is exposed by the inject.js content script.
        for i in range(5):
            expr = f'''(async () => {{
              const t0 = performance.now();
              try {{
                if (!chrome || !chrome.runtime) return "no chrome.runtime";
                const r = await chrome.runtime.sendMessage("pkmdmcklnjdeocoeigmlakhomhhcpafb", {{ type: "log", level: "info", msg: "cdp-ping-{i}", platform: "test" }});
                return JSON.stringify({{ ok: !!r?.ok, ms: (performance.now() - t0).toFixed(1) }});
              }} catch (e) {{
                return JSON.stringify({{ err: e.message, ms: (performance.now() - t0).toFixed(1) }});
              }}
            }})()'''
            r = rpc(ws, 100 + i, "Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, timeout=30)
            v = r.get("result", {}).get("result", {}).get("value", "?")
            print(f"content-tab log ping {i}: {v}")
            time.sleep(0.5)
    finally:
        ws.close()


if __name__ == "__main__":
    main()
