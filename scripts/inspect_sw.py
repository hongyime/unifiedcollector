"""Quick SW state inspection via CDP.

Reads ucLog and ucStatus from chrome.storage.local in the extension SW.
"""
import json
import urllib.request

import websocket


def main() -> None:
    targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/list").read())
    sw = [t for t in targets if t["type"] == "service_worker" and "pkmd" in t["url"]][0]
    ws = websocket.create_connection(sw["webSocketDebuggerUrl"], timeout=8, origin="http://127.0.0.1:9222")
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == 1:
                break
        expr = (
            "chrome.storage.local.get(['ucLog','ucStatus']).then(x => JSON.stringify({"
            "logCount:(x.ucLog||[]).length,"
            "lastLog:(x.ucLog||[]).slice(-6),"
            "status:x.ucStatus||{}"
            "}))"
        )
        ws.send(json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": True, "returnByValue": True},
        }))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == 2:
                value = r.get("result", {}).get("result", {}).get("value", "{}")
                data = json.loads(value)
                print(json.dumps(data, indent=2)[:2400])
                break
    finally:
        ws.close()


if __name__ == "__main__":
    main()
