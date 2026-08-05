"""Inspect current Chrome tab groups via CDP + extension chrome.tabGroups.

Runs in three passes:

1. Ask the browser target for `Target.getTargets` to see what metadata is
   available at the browser-context level (browserContextId, no groupId).
2. Try the CDP method `Target.getBrowserContexts` and the `Browser` /
   `Page` domains for any group-related fields.
3. Evaluate `chrome.tabs.query({})` + `chrome.tabGroups.query({})` inside
   the extension's service worker via CDP by attaching to the SW target.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

import websocket  # type: ignore


def _ws_call(ws: websocket.WebSocket, msg_id: int, method: str, params: dict | None = None) -> Any:
    payload = {"id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    ws.send(json.dumps(payload))
    while True:
        raw = ws.recv()
        data = json.loads(raw)
        if data.get("id") == msg_id:
            return data


def main() -> int:
    ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/version").read())
    browser_ws = ver["webSocketDebuggerUrl"]
    ws = websocket.create_connection(browser_ws)

    # 1. Target.getTargets
    targets = _ws_call(ws, 1, "Target.getTargets")
    pages = [t for t in targets["result"]["targetInfos"] if t.get("type") == "page"]
    print("== Target.getTargets sample (first 3 pages) ==")
    for p in pages[:3]:
        print(json.dumps(p, indent=2))

    keys_seen: set[str] = set()
    for p in pages:
        keys_seen.update(p.keys())
    print("\n== keys seen across all page targets ==")
    print(sorted(keys_seen))

    # 2. Find the extension SW target
    all_targets = targets["result"]["targetInfos"]
    ext_id = "pkmdmcklnjdeocoeigmlakhomhhcpafb"
    sw = next(
        (t for t in all_targets if t.get("type") == "service_worker" and ext_id in t.get("url", "")),
        None,
    )
    if not sw:
        print("\n!! No SW target found for extension", ext_id)
        return 2
    print("\n== SW target ==")
    print(json.dumps(sw, indent=2))

    # Attach to SW via Target.attachToTarget
    attach = _ws_call(ws, 10, "Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True})
    session_id = attach["result"]["sessionId"]

    def sw_call(mid: int, method: str, params: dict | None = None):
        payload = {"id": mid, "method": method, "sessionId": session_id}
        if params is not None:
            payload["params"] = params
        ws.send(json.dumps(payload))
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == mid:
                return data

    # Enable Runtime for SW
    sw_call(11, "Runtime.enable")

    # 3. Query chrome.tabGroups + chrome.tabs
    expr = """
    (async () => {
      const out = { hasTabGroupsApi: typeof chrome !== 'undefined' && !!chrome.tabGroups };
      try {
        out.tabs = await new Promise((res) => chrome.tabs.query({}, res));
      } catch (e) { out.tabs_err = String(e); }
      try {
        if (chrome.tabGroups) {
          out.groups = await new Promise((res) => chrome.tabGroups.query({}, res));
        }
      } catch (e) { out.groups_err = String(e); }
      out.tabsSummary = (out.tabs || []).map(t => ({id:t.id, groupId:t.groupId, url:t.url, index:t.index, windowId:t.windowId}));
      return out;
    })();
    """
    r = sw_call(12, "Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
    print("\n== chrome.tabGroups / chrome.tabs from SW ==")
    print(json.dumps(r, indent=2)[:6000])

    ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
