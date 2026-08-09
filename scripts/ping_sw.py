"""Time a chrome.runtime.sendMessage log ping from tabs.html."""
import io
import json
import sys
import time
import urllib.request

import websocket

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def rpc(ws, request_id, method, params=None):
    body = {"id": request_id, "method": method}
    if params:
        body["params"] = params
    ws.send(json.dumps(body))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == request_id:
            return r


def main():
    ts = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
    tabs = [t for t in ts if "tabs.html" in t.get("url", "")]
    if not tabs:
        print("no tabs.html open; opening one")
        return
    ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=25, origin="http://127.0.0.1:9333")
    try:
        rpc(ws, 1, "Runtime.enable")
        for i in range(5):
            expr = f'''(async () => {{
              const t0 = performance.now();
              try {{
                const r = await chrome.runtime.sendMessage({{ type: "log", level: "info", msg: "sw-ping-{i}", platform: "test" }});
                return JSON.stringify({{ ok: !!r?.ok, ms: (performance.now() - t0).toFixed(1) }});
              }} catch (e) {{
                return JSON.stringify({{ err: e.message, ms: (performance.now() - t0).toFixed(1) }});
              }}
            }})()'''
            r = rpc(ws, 100 + i, "Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
            v = r.get("result", {}).get("result", {}).get("value", "?")
            print(f"log ping {i}: {v}")
            time.sleep(0.3)
    finally:
        ws.close()


if __name__ == "__main__":
    main()
