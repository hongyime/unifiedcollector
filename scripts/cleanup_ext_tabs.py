"""Clean up excess tabs.html option pages — keep only the first one."""
import json
import urllib.request

targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
tabs = [t for t in targets if t.get("type") == "page" and "pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html" in t.get("url", "")]
print(f"found {len(tabs)} tabs.html pages")
# Keep the first one, close the rest
for t in tabs[1:]:
    tid = t["id"]
    r = urllib.request.urlopen(f"http://127.0.0.1:9333/json/close/{tid}").read().decode()
    print(f"closed {tid}: {r}")
