"""Read ucLog & ucStatus via tabs.html chrome.storage (SW may be dormant)."""
import io
import json
import sys
import urllib.request

import websocket

# Force UTF-8 stdout on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


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
    ts = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
    tabs = [t for t in ts if "tabs.html" in t.get("url", "")]
    if not tabs:
        print("no tabs.html found; open chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html first")
        return
    ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=10, origin="http://127.0.0.1:9333")
    try:
        rpc(ws, 1, "Runtime.enable")
        expr = (
            'chrome.storage.local.get(["ucLog","ucStatus"]).then(x => JSON.stringify({'
            'logCount:(x.ucLog||[]).length,'
            'lastLogs:(x.ucLog||[]).slice(-20),'
            'status:x.ucStatus||{}'
            "}))"
        )
        r = rpc(ws, 2, "Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
        v = r.get("result", {}).get("result", {}).get("value", "{}")
        data = json.loads(v)
        print("logCount:", data.get("logCount"))
        print("status:")
        print(json.dumps(data.get("status", {}), indent=2, ensure_ascii=False))
        print("\nlast 20 log entries:")
        for e in data.get("lastLogs", []):
            print(f"  [{e.get('level')}] {e.get('msg')}")
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
