"""Peek threads via browser-level CDP session (works even when tab is busy)."""
import json
import urllib.request
from urllib.parse import urlparse

import websocket  # type: ignore


ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/version").read())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=25)
n = [0]


def is_threads_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "threads.com" or host.endswith(".threads.com")

def call(method, params=None, session=None, timeout=30):
    n[0] += 1
    p = {"id": n[0], "method": method}
    if params is not None:
        p["params"] = params
    if session is not None:
        p["sessionId"] = session
    ws.send(json.dumps(p))
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            ws.settimeout(max(0.5, deadline - _time.time()))
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

a = call("Target.attachToTarget", {"targetId": th["targetId"], "flatten": True})
session = a["result"]["sessionId"]
call("Runtime.enable", session=session)

expr = """
(() => {
  try {
    const body = document.body ? document.body.innerText : '';
    return {
      loc: location.href,
      ttl: document.title,
      body_first_500: body.slice(0, 500),
      articles: document.querySelectorAll('article').length,
      videos: document.querySelectorAll('video').length,
      images: document.querySelectorAll('img[src]').length,
      links: document.querySelectorAll('a[href]').length,
      loginMarker: /log in|sign up|continue with instagram/i.test(body),
      cookie_names: (document.cookie || '').split(';').slice(0, 8).map(c => c.trim().split('=')[0]).join(','),
    };
  } catch (e) {
    return { err: String(e && e.message || e) };
  }
})()
"""
r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=session, timeout=45)
print("full CDP response:")
print(json.dumps(r, indent=2)[:3000])
ws.close()
