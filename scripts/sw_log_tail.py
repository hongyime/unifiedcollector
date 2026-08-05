"""Read the SW's ucLog storage to see recent messages."""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import json
import urllib.request

import websocket  # type: ignore


ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/version").read())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=10)
ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
r = json.loads(ws.recv())
while r.get("id") != 1:
    r = json.loads(ws.recv())

sw = next(
    (t for t in r["result"]["targetInfos"] if t.get("type") == "service_worker" and "pkmd" in t.get("url", "")),
    None,
)
if not sw:
    raise SystemExit("no SW")

ws.send(json.dumps({"id": 2, "method": "Target.attachToTarget", "params": {"targetId": sw["targetId"], "flatten": True}}))
r = json.loads(ws.recv())
while r.get("id") != 2:
    r = json.loads(ws.recv())
session = r["result"]["sessionId"]

ws.send(json.dumps({"id": 3, "method": "Runtime.enable", "sessionId": session}))
while True:
    r = json.loads(ws.recv())
    if r.get("id") == 3:
        break

expr = """
(async () => {
  const { ucLog } = await chrome.storage.local.get('ucLog');
  const arr = ucLog || [];
  return arr.slice(-50);
})()
"""
ws.send(json.dumps({"id": 4, "method": "Runtime.evaluate", "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}, "sessionId": session}))
while True:
    r = json.loads(ws.recv())
    if r.get("id") == 4:
        break
lines = r["result"]["result"]["value"] or []
for l in lines:
    ts = l.get("ts") or ""
    lvl = l.get("level") or ""
    msg = l.get("msg") or ""
    print(f"{ts} {lvl:6s} {msg}")
ws.close()
