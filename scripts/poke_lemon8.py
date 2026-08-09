"""Poke lemon8 tab: force the content script to detect + report recovery.

Runs detectRecoverablePageShell in the tab's content-script world by
messaging the extension. If the content script is loaded, we should see
its response and, importantly, the pageHealth message it sends should
trigger recovery.
"""
import json
import urllib.request

import websocket  # type: ignore

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
lemon = next((t for t in tabs if "lemon8-app" in t.get("url", "") and t.get("type") == "page"), None)
if not lemon:
    raise SystemExit("no lemon8 tab")

ws = websocket.create_connection(lemon["webSocketDebuggerUrl"], timeout=8)
# Content-script world is a separate JS context. isolatedWorldName is 'unified_collector' or similar? Let's discover.
ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
_ = json.loads(ws.recv())
ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
# drain until id=2 reply
while True:
    m = json.loads(ws.recv())
    if m.get("id") == 2:
        break

# Ask CDP for isolated worlds
ws.send(json.dumps({"id": 3, "method": "Page.getFrameTree"}))
while True:
    m = json.loads(ws.recv())
    if m.get("id") == 3:
        break
frame_id = m["result"]["frameTree"]["frame"]["id"]

# Create an isolated world so we can access chrome APIs from the same origin
ws.send(json.dumps({"id": 4, "method": "Page.createIsolatedWorld", "params": {"frameId": frame_id, "worldName": "uc_probe"}}))
while True:
    m = json.loads(ws.recv())
    if m.get("id") == 4:
        break
world_ctx = m["result"]["executionContextId"]

# Now evaluate in main world to see what content-script APIs are available
expr = """(() => {
  const dom = {
    articles: document.querySelectorAll('article').length,
    videos: document.querySelectorAll('video').length,
    images: document.querySelectorAll('img[src]').length,
    links: document.querySelectorAll('a[href]').length,
  };
  const body = (document.body && document.body.innerText) || '';
  const bodyLower = body.toLowerCase();
  return {
    url: location.href,
    dom,
    body_first_200: body.slice(0, 200),
    hasNotFound: /\\bnot\\s+found\\b/i.test(body),
    usefulNodes: dom.articles + dom.videos + dom.images,
    lowContent: (dom.articles + dom.videos + dom.images) < 4 && dom.links < 40,
    // Content-script global? content.js exposes UC_LOOPS or similar (probably not on window)
    windowKeys: Object.keys(window).filter(k => /uc_|_uc|collector/i.test(k)),
  };
})()"""
ws.send(json.dumps({"id": 5, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}}))
while True:
    m = json.loads(ws.recv())
    if m.get("id") == 5:
        break
print("== MAIN world probe ==")
print(json.dumps(m.get("result", {}).get("result", {}).get("value"), indent=2))
ws.close()
