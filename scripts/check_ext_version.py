"""Check version + ping SW from tabs.html option page."""
import json
import urllib.request

import websocket  # type: ignore

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
opt = next(
    t for t in tabs
    if "pkmdmc" in t.get("url", "") and "tabs.html" in t.get("url", "") and t.get("type") == "page"
)
ws = websocket.create_connection(opt["webSocketDebuggerUrl"], timeout=10)

def call(mid, method, params):
    ws.send(json.dumps({"id": mid, "method": method, "params": params}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == mid:
            return r


r = call(1, "Runtime.evaluate", {"expression": "chrome.runtime.getManifest().version", "returnByValue": True})
print("version from tabs.html:", r.get("result", {}).get("result", {}).get("value"))

# Ping the SW via chrome.runtime.sendMessage
expr = """
(async () => {
  try {
    const r = await chrome.runtime.sendMessage({ type: 'popup_status_request' });
    return { ok: true, r };
  } catch (e) {
    return { ok: false, err: String(e && e.message || e) };
  }
})()
"""
r = call(2, "Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
print("SW ping:", json.dumps(r.get("result", {}).get("result", {}).get("value"), indent=2))
