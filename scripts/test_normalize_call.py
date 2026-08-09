"""Directly test shouldNormalizeSingleFeedTab against the current lemon8 tab."""
import json
import time
import urllib.request

import websocket  # type: ignore


ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/version").read())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=10)
n = [0]

def call(method, params=None, session=None):
    n[0] += 1
    p = {"id": n[0], "method": method}
    if params is not None:
        p["params"] = params
    if session is not None:
        p["sessionId"] = session
    ws.send(json.dumps(p))
    while True:
        d = json.loads(ws.recv())
        if d.get("id") == n[0]:
            return d


targets = call("Target.getTargets")["result"]["targetInfos"]
sw = next((t for t in targets if t.get("type") == "service_worker" and "pkmd" in t.get("url", "")), None)
if not sw:
    # Wake via tabs.html
    req = urllib.request.Request("http://127.0.0.1:9333/json/new?chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html", method="PUT")
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
  const ver = chrome.runtime.getManifest().version;
  const p = scraperPlatforms().find(x => x.id === 'lemon8');
  const tabs = await chrome.tabs.query({ url: platformUrlPatterns(p) });
  const results = [];
  for (const t of tabs) {
    const shouldNorm = shouldNormalizeSingleFeedTab(p, t, 'manual_extension_reload');
    results.push({ tab_url: t.url, groupId: t.groupId, shouldNorm });
  }
  return {
    ver,
    platform_url: p && p.url,
    normalize_platforms_has_lemon8: (function(){
      try {
        return String(shouldNormalizeSingleFeedTab.toString()).includes("lemon8");
      } catch (e) { return null; }
    })(),
    fn_source_first_400: String(shouldNormalizeSingleFeedTab.toString()).slice(0, 400),
    results,
  };
})()
"""
r = call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, session=session)
print(json.dumps(r.get("result", {}).get("result", {}).get("value"), indent=2))
ws.close()
