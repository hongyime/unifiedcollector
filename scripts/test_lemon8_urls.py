"""Test multiple lemon8 URL candidates for the actual feed page."""
import json
import time
import urllib.request

import websocket  # type: ignore

candidates = [
    "https://www.lemon8-app.com/topic/food",
    "https://www.lemon8-app.com/topic/food?region=sg",
    "https://www.lemon8-app.com/topic/fashion?region=sg",
    "https://www.lemon8-app.com/topic/lifestyle?region=sg",
    "https://www.lemon8-app.com/topic/beauty?region=sg",
    "https://www.lemon8-app.com/topic/travel?region=sg",
    "https://www.lemon8-app.com/discover?region=sg",
    "https://www.lemon8-app.com/explore?region=sg",
    "https://www.lemon8-app.com/trending?region=sg",
]

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
lemon = next(t for t in tabs if "lemon8-app" in t.get("url", "") and t.get("type") == "page")
ws = websocket.create_connection(lemon["webSocketDebuggerUrl"], timeout=8)

n = [10]

def call(m, p=None):
    n[0] += 1
    ws.send(json.dumps({"id": n[0], "method": m, "params": p or {}}))
    while True:
        d = json.loads(ws.recv())
        if d.get("id") == n[0]:
            return d


call("Page.enable")

expr = """({
  loc: location.href,
  ttl: document.title,
  txt_first_150: (document.body ? document.body.innerText : '').slice(0, 150),
  imgs: document.querySelectorAll('img[src]').length,
  articles: document.querySelectorAll('article').length,
  links: document.querySelectorAll('a[href]').length,
  has_next_data: !!document.getElementById('__NEXT_DATA__'),
  hasNotFound: /\\bnot\\s+found\\b/i.test((document.body ? document.body.innerText : '')),
  cards: document.querySelectorAll('[class*="card"], [class*="post"], [href*="/post/"], [href*="/@"]').length,
})"""

for url in candidates:
    call("Page.navigate", {"url": url})
    time.sleep(4)
    r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    val = r.get("result", {}).get("result", {}).get("value") or {}
    val["_tested"] = url
    # compact one-line
    print(f"{url:60s} imgs={val.get('imgs'):>3} articles={val.get('articles'):>2} links={val.get('links'):>3} cards={val.get('cards'):>3} nd={val.get('has_next_data')} nf={val.get('hasNotFound')} loc={val.get('loc')}")

ws.close()
