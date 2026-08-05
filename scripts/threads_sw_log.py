"""Ask the SW for the current threads tab health via ucLog scan."""
import json
import time
import urllib.request

import websocket  # type: ignore


ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/version").read())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=15)
n = [0]

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
sw = next((t for t in targets if t.get("type") == "service_worker" and "pkmd" in t.get("url", "")), None)
if not sw:
    req = urllib.request.Request("http://127.0.0.1:9222/json/new?chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html", method="PUT")
    urllib.request.urlopen(req).read()
    time.sleep(5)
    targets = call("Target.getTargets")["result"]["targetInfos"]
    sw = next((t for t in targets if t.get("type") == "service_worker" and "pkmd" in t.get("url", "")), None)
if not sw:
    raise SystemExit("no SW")

a = call("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True})
session = a["result"]["sessionId"]
call("Runtime.enable", session=session)

expr = """
(async () => {
  const { ucLog } = await chrome.storage.local.get('ucLog');
  const arr = ucLog || [];
  return arr.filter(l => /threads|forced_cycle|hard_refresh|threads_posts|posts endpoint/i.test(l.msg || '')).slice(-50);
})()
"""
r = call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, session=session)
lines = r.get("result", {}).get("result", {}).get("value") or []
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
for l in lines:
    print(f"{l.get('ts', '')[:19]} {l.get('level', ''):6s} {l.get('msg', '')}")
ws.close()
