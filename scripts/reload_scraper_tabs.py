"""Reload all Chrome scraper tabs so the fresh extension content scripts get injected."""
import io
import json
import sys
import time
import urllib.request

import websocket

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SCRAPER_HOSTS = (
    "www.instagram.com",
    "www.threads.com",
    "www.tiktok.com",
    "www.lemon8-app.com",
    "x.com",
    "twitter.com",
    "www.facebook.com",
    "www.strava.com",
)


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
    ts = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/list").read())
    scraper_tabs = [t for t in ts if t.get("type") == "page" and any(h in t.get("url", "") for h in SCRAPER_HOSTS)]
    print(f"found {len(scraper_tabs)} scraper tabs")
    for t in scraper_tabs:
        url = t.get("url", "")[:70]
        try:
            ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=6, origin="http://127.0.0.1:9222")
            rpc(ws, 1, "Page.enable")
            rpc(ws, 2, "Page.reload", {"ignoreCache": False})
            ws.close()
            print(f"  reloaded: {url}")
        except Exception as e:
            print(f"  FAIL {url}: {e}")
        time.sleep(1.5)  # small stagger


if __name__ == "__main__":
    main()
