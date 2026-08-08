"""Force-reload the threads tab via CDP to get its content script fresh."""
import json
import time
import urllib.request
from urllib.parse import urlparse

import websocket  # type: ignore


ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/version").read())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=15)
n = [0]


def is_threads_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "threads.com" or host.endswith(".threads.com")

def call(method, params=None, session=None, timeout=15):
    n[0] += 1
    p = {"id": n[0], "method": method}
    if params is not None:
        p["params"] = params
    if session is not None:
        p["sessionId"] = session
    ws.send(json.dumps(p))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ws.settimeout(max(0.5, deadline - time.time()))
            d = json.loads(ws.recv())
        except Exception:
            continue
        if d.get("id") == n[0]:
            return d
    return {"error": "timeout"}


targets = call("Target.getTargets")["result"]["targetInfos"]
th = next((t for t in targets if t.get("type") == "page" and is_threads_url(t.get("url", ""))), None)
if not th:
    raise SystemExit("no threads tab")
print("threads tab url:", th["url"])

a = call("Target.attachToTarget", {"targetId": th["targetId"], "flatten": True})
if "error" in a:
    print("attach failed:", a["error"])
    raise SystemExit
session = a["result"]["sessionId"]
r = call("Page.reload", {"ignoreCache": True}, session=session, timeout=20)
print("reload:", r.get("result", r.get("error")))
ws.close()
