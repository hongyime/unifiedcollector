"""Probe what tabs.group() can do WITHOUT the tabGroups permission.

We already know from inspect_tab_groups.py:
  - hasTabGroupsApi: false  (chrome.tabGroups namespace missing)
  - chrome.tabs.query returns groupId on each tab

Question: can chrome.tabs.group({tabIds:[x], groupId:GID}) succeed with
only "tabs" permission, or does it require "tabGroups"?

Approach: from SW, take one existing social tab (index=0, tiktok/foryou)
and call chrome.tabs.group({tabIds:[id], groupId:currentGroupId}). This
is a no-op (tab already in that group) so it should succeed silently if
allowed, or throw a permission error.
"""
from __future__ import annotations

import json
import sys
import urllib.request

import websocket  # type: ignore


def main() -> int:
    ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/version").read())
    ws = websocket.create_connection(ver["webSocketDebuggerUrl"])

    def _call(mid: int, method: str, params: dict | None = None, session_id: str | None = None):
        payload = {"id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        if session_id is not None:
            payload["sessionId"] = session_id
        ws.send(json.dumps(payload))
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == mid:
                return data

    targets = _call(1, "Target.getTargets")
    ext_id = "pkmdmcklnjdeocoeigmlakhomhhcpafb"
    sw = next(
        (
            t
            for t in targets["result"]["targetInfos"]
            if t.get("type") == "service_worker" and ext_id in t.get("url", "")
        ),
        None,
    )
    if not sw:
        print("SW not found")
        return 2

    attach = _call(2, "Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True})
    session_id = attach["result"]["sessionId"]
    _call(3, "Runtime.enable", session_id=session_id)

    expr = """
    (async () => {
      const tabs = await new Promise((r) => chrome.tabs.query({}, r));
      const social = tabs.find(t => t.url && /tiktok|instagram|threads|facebook|lemon8|x\\.com|strava/.test(t.url));
      if (!social) return { err: 'no social tab' };
      const gid = social.groupId;
      try {
        const r = await new Promise((res, rej) => {
          try {
            chrome.tabs.group({ tabIds: [social.id], groupId: gid }, (id) => {
              if (chrome.runtime.lastError) rej(new Error(chrome.runtime.lastError.message));
              else res(id);
            });
          } catch (e) { rej(e); }
        });
        return { ok: true, groupId: gid, result: r, tabId: social.id, url: social.url };
      } catch (e) {
        return { ok: false, groupId: gid, err: String(e), tabId: social.id, url: social.url };
      }
    })();
    """
    r = _call(4, "Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, session_id=session_id)
    print(json.dumps(r, indent=2))
    ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
