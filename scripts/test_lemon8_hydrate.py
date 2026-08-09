"""Wait longer for lemon8 SPA to hydrate at /foryou."""
import json
import time
import urllib.request

import websocket  # type: ignore

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
lemon = next(t for t in tabs if "lemon8-app" in t.get("url", "") and t.get("type") == "page")
ws = websocket.create_connection(lemon["webSocketDebuggerUrl"], timeout=8)
n = [0]

def call(m, p=None):
    n[0] += 1
    ws.send(json.dumps({"id": n[0], "method": m, "params": p or {}}))
    while True:
        d = json.loads(ws.recv())
        if d.get("id") == n[0]:
            return d


call("Page.enable")
call("Page.navigate", {"url": "https://www.lemon8-app.com/foryou"})

expr = """({
  loc: location.href,
  ttl: document.title,
  imgs: document.querySelectorAll('img[src]').length,
  articles: document.querySelectorAll('article').length,
  links: document.querySelectorAll('a[href]').length,
  has_next_data: !!document.getElementById('__NEXT_DATA__'),
  hasNotFound: /\\bnot\\s+found\\b/i.test((document.body ? document.body.innerText : '')),
  posts: document.querySelectorAll('a[href*="/@"], a[href*="/post/"]').length,
  cdn_imgs: [...document.querySelectorAll('img[src]')].filter(i => /byteimg|ibytedtos|muscdn|lemon8|tos-/i.test(i.src)).length,
})"""

# poll every 5 seconds up to 45 seconds
for i in range(9):
    time.sleep(5)
    r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    val = r.get("result", {}).get("result", {}).get("value") or {}
    print(f"t={(i+1)*5:>2}s loc={val.get('loc'):55s} imgs={val.get('imgs'):>3} arti={val.get('articles'):>2} cdn={val.get('cdn_imgs'):>3} posts={val.get('posts'):>3} nd={val.get('has_next_data')} nf={val.get('hasNotFound')}")

ws.close()
