"""Force-reload the extension by evaluating chrome.runtime.reload() in the SW."""
import json
import time
import urllib.request

import websocket


def get_sw():
    targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/list").read())
    for t in targets:
        if t["type"] == "service_worker" and "pkmd" in t["url"]:
            return t
    return None


def rpc(ws, request_id, method, params=None):
    body = {"id": request_id, "method": method}
    if params:
        body["params"] = params
    ws.send(json.dumps(body))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == request_id:
            return r


def main():
    sw = get_sw()
    if not sw:
        print("no SW target found; opening extension page to wake it")
        # Open a data: URL as a wake nudge
        return
    ws = websocket.create_connection(sw["webSocketDebuggerUrl"], timeout=8, origin="http://127.0.0.1:9222")
    rpc(ws, 1, "Runtime.enable")
    version = rpc(ws, 2, "Runtime.evaluate", {
        "expression": "chrome.runtime.getManifest().version",
        "returnByValue": True,
    })
    print("pre-reload version:", version.get("result", {}).get("result", {}).get("value"))
    # Fire reload; the SW will drop and Chrome will reconstruct a fresh one.
    try:
        ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate",
                            "params": {"expression": "chrome.runtime.reload()"}}))
    except Exception as e:
        print("send failed:", e)
    time.sleep(2)
    try:
        ws.close()
    except Exception:
        pass
    print("reload issued; waiting 8s for new SW...")
    time.sleep(8)
    sw2 = get_sw()
    if not sw2:
        print("no SW target after reload; extension may need a page load to wake it")
        return
    ws2 = websocket.create_connection(sw2["webSocketDebuggerUrl"], timeout=8, origin="http://127.0.0.1:9222")
    rpc(ws2, 1, "Runtime.enable")
    v = rpc(ws2, 2, "Runtime.evaluate", {
        "expression": "chrome.runtime.getManifest().version",
        "returnByValue": True,
    })
    print("post-reload version:", v.get("result", {}).get("result", {}).get("value"))
    ws2.close()


if __name__ == "__main__":
    main()
