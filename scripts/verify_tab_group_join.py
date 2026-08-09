"""Verify tab-group awareness end-to-end.

1. Read all current scraper tab groupIds via SW.
2. Simulate a "recovery open" by calling createTabInSocialGroup with a
   test URL that matches a scraper host (facebook), then read its
   groupId. Cleanup: close the test tab.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

import websocket  # type: ignore


def _cdp_session():
    ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/version").read())
    ws = websocket.create_connection(ver["webSocketDebuggerUrl"])
    msg_id = [0]

    def _call(method: str, params: dict | None = None, session_id: str | None = None):
        msg_id[0] += 1
        payload = {"id": msg_id[0], "method": method}
        if params is not None:
            payload["params"] = params
        if session_id is not None:
            payload["sessionId"] = session_id
        ws.send(json.dumps(payload))
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == msg_id[0]:
                return data

    return ws, _call


def _attach_sw(_call):
    targets = _call("Target.getTargets")
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
        raise SystemExit("SW target not found")
    attach = _call("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True})
    session_id = attach["result"]["sessionId"]
    _call("Runtime.enable", session_id=session_id)
    return session_id


def _eval(_call, session_id, expr):
    r = _call(
        "Runtime.evaluate",
        {"expression": expr, "awaitPromise": True, "returnByValue": True},
        session_id=session_id,
    )
    if "error" in r:
        raise SystemExit(f"eval error: {r}")
    return r["result"]["result"].get("value")


def main() -> int:
    ws, call = _cdp_session()
    session = _attach_sw(call)

    # 1. Grab current group state
    before = _eval(call, session, """
    (async () => {
      const tabs = await new Promise((r) => chrome.tabs.query({}, r));
      const scraper = tabs.filter(t => t.url && /tiktok|instagram|threads|facebook|lemon8|x\\.com|strava/.test(t.url));
      const groups = new Set(scraper.map(t => t.groupId).filter(g => g >= 0));
      return { groups: [...groups], scraperCount: scraper.length, scraperGroups: scraper.map(t => ({url:t.url, groupId:t.groupId})) };
    })();
    """)
    print("== Existing scraper tabs group state ==")
    print(json.dumps(before, indent=2))
    if not before["groups"]:
        print("!! no groupIds detected on existing scraper tabs — user hasn't grouped anything, nothing to test")
        return 1

    expected_gid = before["groups"][0]
    print(f"\nExpected groupId for a new recovery tab: {expected_gid}")

    # 2. Simulate a "recovery open" via createTabInSocialGroup with a benign URL.
    # We use https://www.facebook.com/help/ so we don't interfere with the scraper.
    test = _eval(call, session, """
    (async () => {
      const opts = { url: 'https://www.facebook.com/help/', pinned: false, active: false };
      const created = await createTabInSocialGroup(opts);
      // Give Chrome a moment to reflect the group assignment.
      await new Promise(r => setTimeout(r, 500));
      const after = await new Promise(r => chrome.tabs.get(created.id, r));
      return { createdId: created.id, url: after.url, groupId: after.groupId, windowId: after.windowId };
    })();
    """)
    print("\n== New test tab (should share groupId) ==")
    print(json.dumps(test, indent=2))

    ok = test and test.get("groupId") == expected_gid
    print(f"\n== VERDICT: {'PASS — new tab joined the social group' if ok else 'FAIL — new tab NOT in social group'}")

    # 3. Cleanup
    if test and test.get("createdId"):
        _eval(call, session, f"""
        (async () => {{
          await new Promise(r => chrome.tabs.remove({test['createdId']}, r));
        }})();
        """)
        print(f"cleanup: closed test tab {test['createdId']}")

    ws.close()
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
