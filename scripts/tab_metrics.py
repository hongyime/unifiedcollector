"""Print per-tab CDP performance metrics."""
import io
import json
import sys
import urllib.request

import websocket

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


def main() -> None:
    ts = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
    pages = [t for t in ts if t.get("type") == "page"]
    print(f"{'URL':<75}  {'JS MB':>7}  {'Docs':>5}  {'Nodes':>7}  {'Frames':>7}")
    total_heap = 0.0
    for t in pages:
        url = t.get("url", "")[:75]
        try:
            ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=5, origin="http://127.0.0.1:9333")
            rpc(ws, 1, "Performance.enable")
            r = rpc(ws, 2, "Performance.getMetrics")
            metrics = {m["name"]: m["value"] for m in r["result"]["metrics"]}
            heap_mb = metrics.get("JSHeapUsedSize", 0) / 1024 / 1024
            docs = int(metrics.get("Documents", 0))
            nodes = int(metrics.get("Nodes", 0))
            frames = int(metrics.get("Frames", 0))
            total_heap += heap_mb
            print(f"{url:<75}  {heap_mb:>7.1f}  {docs:>5}  {nodes:>7}  {frames:>7}")
            ws.close()
        except Exception as e:
            print(f"{url:<75}  ERR: {e}")
    print(f"\ntotal JS heap across tabs: {total_heap:.1f} MB")


if __name__ == "__main__":
    main()
