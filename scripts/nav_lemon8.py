"""Navigate the lemon8 tab to the base URL and inspect post-nav state."""
import json
import time
import urllib.request

import websocket  # type: ignore

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
lemon = next(t for t in tabs if "lemon8-app" in t.get("url", "") and t.get("type") == "page")

ws = websocket.create_connection(lemon["webSocketDebuggerUrl"], timeout=8)
ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
_ = json.loads(ws.recv())

ws.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": "https://www.lemon8-app.com/"}}))
r = json.loads(ws.recv())
while r.get("id") != 2:
    r = json.loads(ws.recv())
print("nav:", r.get("result"))

time.sleep(10)

expr = """({
  loc: location.href,
  ttl: document.title,
  txt: (document.body ? document.body.innerText : '').slice(0, 500),
  imgs: document.querySelectorAll('img[src]').length,
  articles: document.querySelectorAll('article').length,
  links: document.querySelectorAll('a[href]').length,
  has_next_data: !!document.getElementById('__NEXT_DATA__'),
})"""
ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}}))
r = json.loads(ws.recv())
while r.get("id") != 3:
    r = json.loads(ws.recv())
print("post-nav state:")
print(json.dumps(r.get("result", {}).get("result", {}).get("value"), indent=2))
ws.close()
