"""Force ensureScraperTabsOpen from SW so the wandering lemon8 tab snaps back."""
import json
import time
import urllib.request

import websocket  # type: ignore

from cdp_ext_tabs import open_or_activate_control_tab


def _find_sw():
    ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/version").read())
    ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=10)
    ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
    r = json.loads(ws.recv())
    while r.get("id") != 1:
        r = json.loads(ws.recv())
    sw = next(
        (t for t in r["result"]["targetInfos"] if t.get("type") == "service_worker" and "pkmd" in t.get("url", "")),
        None,
    )
    return ws, sw


ws, sw = _find_sw()
if not sw:
    # Wake via tabs.html
    open_or_activate_control_tab()
    time.sleep(6)
    ws, sw = _find_sw()

if not sw:
    raise SystemExit("SW not found even after wake")

n = [10]

def call(method, params=None, session_id=None):
    n[0] += 1
    payload = {"id": n[0], "method": method}
    if params is not None:
        payload["params"] = params
    if session_id is not None:
        payload["sessionId"] = session_id
    ws.send(json.dumps(payload))
    while True:
        d = json.loads(ws.recv())
        if d.get("id") == n[0]:
            return d


attach = call("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True})
session = attach["result"]["sessionId"]
call("Runtime.enable", session_id=session)

# Directly invoke ensureScraperTabsOpen("manual_extension_reload", {force:true})
expr = """
(async () => {
  try {
    // Reset the in-flight guard in case a prior sweep is stuck.
    _tabsOpInProgress = false;
    await ensureScraperTabsOpen("manual_extension_reload", { force: true });
    return { ok: true };
  } catch (e) {
    return { ok: false, err: String(e && e.message || e) };
  }
})()
"""
r = call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, session_id=session)
print(json.dumps(r.get("result", {}).get("result", {}).get("value"), indent=2))
ws.close()
