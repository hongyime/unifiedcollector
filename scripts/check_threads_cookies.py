"""Read cookies for threads.com to check login state without touching the tab."""
import json
import time
import urllib.request

import websocket  # type: ignore


ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/version").read())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=15)
n = [0]

def call(method, params=None, timeout=15):
    n[0] += 1
    p = {"id": n[0], "method": method}
    if params is not None:
        p["params"] = params
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


r = call("Storage.getCookies")
if "error" in r:
    print("failed:", r)
    raise SystemExit
cookies = r["result"]["cookies"]
threads_cookies = [c for c in cookies if "threads.com" in (c.get("domain", ""))]
print(f"threads.com cookies: {len(threads_cookies)}")
important = ["sessionid", "ig_did", "csrftoken", "ds_user_id"]
for c in threads_cookies:
    name = c.get("name")
    val = c.get("value") or ""
    dom = c.get("domain")
    exp = c.get("expires")
    if name in important or "user" in (name or "").lower():
        print(f"  {name}={val[:12] + '...' if len(val) > 12 else val} domain={dom} expires={exp}")
    else:
        print(f"  {name}=<hidden> domain={dom}")
ws.close()
