"""Force extension reload by calling chrome.runtime.reload() from tabs.html.

The SW may be dead/suspended, but tabs.html is an extension page and can
call chrome.runtime.reload() directly. That tears down the extension and
Chrome re-reads the manifest from disk.
"""
import json
import time
import urllib.request

import websocket  # type: ignore


def _find_tabs_html():
    tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
    return next(
        (t for t in tabs if t.get("type") == "page" and "pkmdmc" in t.get("url", "") and "tabs.html" in t.get("url", "")),
        None,
    )


opt = _find_tabs_html()
if not opt:
    raise SystemExit("no tabs.html — open the extension options page first")
ws = websocket.create_connection(opt["webSocketDebuggerUrl"], timeout=10)

def call(mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == mid:
            return r


pre = call(1, "Runtime.evaluate", {"expression": "chrome.runtime.getManifest().version", "returnByValue": True})
print("pre-reload version:", pre.get("result", {}).get("result", {}).get("value"))

# Fire reload; tabs.html will be destroyed as the extension unloads.
try:
    call(2, "Runtime.evaluate", {"expression": "chrome.runtime.reload()", "returnByValue": True})
except Exception as e:
    print("reload eval (expected disconnect):", e)
try:
    ws.close()
except Exception:
    pass

print("reload issued; waiting 10s for Chrome to rebuild...")
time.sleep(10)

opt2 = _find_tabs_html()
if not opt2:
    print("no tabs.html after reload — opening one")
    req = urllib.request.Request(
        "http://127.0.0.1:9333/json/new?chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html",
        method="PUT",
    )
    urllib.request.urlopen(req).read()
    time.sleep(6)
    opt2 = _find_tabs_html()

if not opt2:
    print("still no tabs.html — extension may be disabled")
    raise SystemExit(1)

ws2 = websocket.create_connection(opt2["webSocketDebuggerUrl"], timeout=10)
def call2(mid, method, params=None):
    ws2.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws2.recv())
        if r.get("id") == mid:
            return r

post = call2(1, "Runtime.evaluate", {"expression": "chrome.runtime.getManifest().version", "returnByValue": True})
print("post-reload version:", post.get("result", {}).get("result", {}).get("value"))

# Verify SW is now responsive
ping_expr = """
(async () => {
  try {
    const r = await Promise.race([
      chrome.runtime.sendMessage({ type: "log", level: "info", msg: "cdp_reload_probe" }),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout8s")), 8000)),
    ]);
    return { ok: true, r };
  } catch (e) {
    return { ok: false, err: String(e && e.message || e) };
  }
})()
"""
p = call2(2, "Runtime.evaluate", {"expression": ping_expr, "returnByValue": True, "awaitPromise": True})
print("SW ping:", json.dumps(p.get("result", {}).get("result", {}).get("value"), indent=2))
ws2.close()
