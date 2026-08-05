"""Check the current state of the lemon8 tab."""
import json
import urllib.request
import websocket  # type: ignore

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/list").read())
lemon = [t for t in tabs if "lemon8-app" in t.get("url", "") and t.get("type") == "page"]
for t in lemon:
    ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=5)
    expr = "({loc: location.href, ttl: document.title, txt: (document.body ? document.body.innerText : '').slice(0, 400)})"
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}}))
    r = json.loads(ws.recv())
    while r.get("id") != 1:
        r = json.loads(ws.recv())
    print(json.dumps(r.get("result", {}).get("result", {}).get("value"), indent=2))
    ws.close()
