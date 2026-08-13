"""Clean up excess UnifiedCollector extension control pages.

Only closes chrome-extension://*/tabs.html targets for the known Collector
extension IDs. Platform/login tabs are left untouched.
"""
import json
import socket
import time
import urllib.parse
import urllib.request

CDP = "http://127.0.0.1:9333"
PRIMARY_ID = "pkmdmcklnjdeocoeigmlakhomhhcpafb"
KNOWN_IDS = {PRIMARY_ID, "nkeimhogjdpnpccoofpliimaahmaaome"}


def load_targets():
    try:
        with urllib.request.urlopen(f"{CDP}/json/list", timeout=5) as resp:
            return json.loads(resp.read())
    except (TimeoutError, socket.timeout) as exc:
        print(f"CDP target list timed out; stopping cleanup pass: {exc}")
        return None


def is_control_page(target):
    url = target.get("url") or ""
    if target.get("type") != "page":
        return False
    return any(url.startswith(f"chrome-extension://{ext_id}/tabs.html") for ext_id in KNOWN_IDS)


def is_blank_page(target):
    url = target.get("url") or ""
    return target.get("type") == "page" and url in {"about:blank", "chrome://newtab/"}


def close_target(target):
    target_id = urllib.parse.quote(str(target["id"]), safe="")
    with urllib.request.urlopen(f"{CDP}/json/close/{target_id}", timeout=5) as resp:
        return resp.read().decode(errors="replace").strip()


closed = 0
for pass_no in range(3):
    targets = load_targets()
    if targets is None:
        break
    tabs = [t for t in targets if is_control_page(t)]
    primary = [t for t in tabs if (t.get("url") or "").startswith(f"chrome-extension://{PRIMARY_ID}/tabs.html")]
    keep = sorted(primary or tabs, key=lambda t: str(t.get("id") or ""))[:1]
    keep_id = keep[0].get("id") if keep else None
    to_close = [t for t in tabs if t.get("id") != keep_id]
    print(f"pass {pass_no + 1}: found {len(tabs)} control pages, closing {len(to_close)}")
    for target in to_close:
        try:
            result = close_target(target)
            closed += 1
            print(f"closed {target.get('id')}: {result}")
        except Exception as exc:
            print(f"could not close {target.get('id')}: {exc}")
    blank_tabs = [t for t in targets if is_blank_page(t)] if tabs else []
    if blank_tabs:
        print(f"pass {pass_no + 1}: found {len(blank_tabs)} blank/newtab page(s), closing")
    for target in blank_tabs:
        try:
            result = close_target(target)
            closed += 1
            print(f"closed blank/newtab {target.get('id')}: {result}")
        except Exception as exc:
            print(f"could not close blank/newtab {target.get('id')}: {exc}")
    if not to_close and not blank_tabs:
        break
    time.sleep(1)

print(f"closed_total {closed}")
