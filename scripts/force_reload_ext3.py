"""Wake+reload the extension SW robustly.

Chrome MV3 SWs sleep after idle; direct WS attach can hang. Use
Target.setAutoAttach so any newly spawned SW attaches; POST a wake
message via /json/list; then eval chrome.runtime.reload() in the SW.
"""
import json
import sys
import time
import urllib.request

import websocket  # type: ignore


EXT_ID = "pkmdmcklnjdeocoeigmlakhomhhcpafb"


def main():
    ver = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/version").read())
    ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=10)
    n = [0]

    def call(method, params=None, session_id=None, timeout=8):
        n[0] += 1
        payload = {"id": n[0], "method": method}
        if params is not None:
            payload["params"] = params
        if session_id is not None:
            payload["sessionId"] = session_id
        ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ws.settimeout(deadline - time.time())
                d = json.loads(ws.recv())
            except Exception as e:
                return {"error": {"message": f"recv timeout: {e}"}}
            if d.get("id") == n[0]:
                return d
        return {"error": {"message": "no reply"}}

    # Wake the SW by opening the tabs.html — this forces Chrome to spin up the SW
    # if it was inactive. Silent no-op if the tab already exists.
    call("Target.createTarget", {"url": f"chrome-extension://{EXT_ID}/tabs.html"})
    time.sleep(3)

    targets = call("Target.getTargets")["result"]["targetInfos"]
    sw = next(
        (t for t in targets if t.get("type") == "service_worker" and EXT_ID in t.get("url", "")),
        None,
    )
    if not sw:
        print("no SW after wake attempt — bailing", file=sys.stderr)
        return

    attach = call("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True}, timeout=12)
    if "error" in attach:
        print("attach failed:", attach["error"], file=sys.stderr)
        return
    session = attach["result"]["sessionId"]

    # Runtime.enable can hang if SW is truly cold; give it a longer window.
    r = call("Runtime.enable", session_id=session, timeout=15)
    if "error" in r:
        print("Runtime.enable failed:", r["error"], file=sys.stderr)
        return

    v = call("Runtime.evaluate", {"expression": "chrome.runtime.getManifest().version", "returnByValue": True}, session_id=session)
    pre = v.get("result", {}).get("result", {}).get("value")
    print("pre-reload version:", pre)

    # Fire reload; the SW drops immediately. Do NOT wait for a response.
    n[0] += 1
    ws.send(json.dumps({
        "id": n[0], "method": "Runtime.evaluate",
        "params": {"expression": "chrome.runtime.reload()"},
        "sessionId": session,
    }))
    time.sleep(1.5)
    print("reload issued; waiting 6s for the new SW to be built...")
    time.sleep(6)

    # Wake the new SW and re-attach
    call("Target.createTarget", {"url": f"chrome-extension://{EXT_ID}/tabs.html?ver_check=1"})
    time.sleep(4)
    targets = call("Target.getTargets")["result"]["targetInfos"]
    sw2 = next(
        (t for t in targets if t.get("type") == "service_worker" and EXT_ID in t.get("url", "")),
        None,
    )
    if not sw2:
        print("no fresh SW; extension version confirmation skipped", file=sys.stderr)
        return
    attach2 = call("Target.attachToTarget", {"targetId": sw2["targetId"], "flatten": True}, timeout=12)
    if "error" in attach2:
        print("attach2 failed:", attach2["error"], file=sys.stderr)
        return
    session2 = attach2["result"]["sessionId"]
    call("Runtime.enable", session_id=session2, timeout=15)
    v2 = call("Runtime.evaluate", {"expression": "chrome.runtime.getManifest().version", "returnByValue": True}, session_id=session2)
    post = v2.get("result", {}).get("result", {}).get("value")
    print("post-reload version:", post)
    ws.close()


if __name__ == "__main__":
    main()
