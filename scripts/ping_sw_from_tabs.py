"""Ping the SW from tabs.html and read the running manifest version."""
import json
import urllib.request

import websocket  # type: ignore

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
opt = next(
    (t for t in tabs if t.get("type") == "page" and "pkmdmc" in t.get("url", "") and "tabs.html" in t.get("url", "")),
    None,
)
if not opt:
    raise SystemExit("no tabs.html")
ws = websocket.create_connection(opt["webSocketDebuggerUrl"], timeout=10)

def call(mid, expr, await_promise=False):
    ws.send(json.dumps({
        "id": mid, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
    }))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == mid:
            return r


v = call(1, "chrome.runtime.getManifest().version")
print("tabs.html manifest version:", v["result"]["result"]["value"])

ping_expr = """
(async () => {
  try {
    const p = chrome.runtime.sendMessage({ type: "log", level: "info", msg: "tabsHtmlPingFromCDP" });
    const r = await Promise.race([
      p,
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout5s")), 5000)),
    ]);
    return { ok: true, r };
  } catch (e) {
    return { ok: false, err: String(e && e.message || e) };
  }
})()
"""
p = call(2, ping_expr, await_promise=True)
print("SW ping:", json.dumps(p.get("result", {}).get("result", {}).get("value"), indent=2))
ws.close()
