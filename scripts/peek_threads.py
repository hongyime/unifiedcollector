"""Inspect the live threads tab: DOM content, login status, recoverable-shell probe."""
import json
import urllib.request
from urllib.parse import urlparse

import websocket  # type: ignore


def is_threads_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "threads.com" or host.endswith(".threads.com")


tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
th = next(
    (t for t in tabs if is_threads_url(t.get("url", "")) and t.get("type") == "page"),
    None,
)
if not th:
    raise SystemExit("no threads tab")
ws = websocket.create_connection(th["webSocketDebuggerUrl"], timeout=25)
ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
    "expression": """({
  loc: location.href,
  ttl: document.title,
  txt_first_400: (document.body ? document.body.innerText : '').slice(0, 400),
  articles: document.querySelectorAll('article').length,
  videos: document.querySelectorAll('video').length,
  images: document.querySelectorAll('img[src]').length,
  links: document.querySelectorAll('a[href]').length,
  status_pill_present: !!document.querySelector('[data-e2e*="status"], [role="alert"]'),
  login_marker: /log\\s*in|sign\\s*up|continue\\s+with\\s+instagram/i.test((document.body ? document.body.innerText : '')),
  cookies_hint: document.cookie ? document.cookie.split(';').slice(0,5).map(c => c.trim().split('=')[0]).join(',') : 'no-cookies',
  post_links_count: document.querySelectorAll('a[href*="/@"], a[href*="/post/"]').length,
})""",
    "returnByValue": True,
}}))
r = json.loads(ws.recv())
while r.get("id") != 1:
    r = json.loads(ws.recv())
print(json.dumps(r.get("result", {}).get("result", {}).get("value"), indent=2))
ws.close()
