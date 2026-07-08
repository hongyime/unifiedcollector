import httpx, urllib.parse, glob, os, sys


def _discover_cookies_path() -> str:
    """Match src/collectors/tiktok/__init__.py::_discover_cookie_file semantics
    so this probe uses the same cookie file the live collector picks."""
    candidates: list[str] = []
    for d in ("/app/credentials/tiktok", "credentials/tiktok"):
        candidates.extend(glob.glob(os.path.join(d, "tiktok_*.txt")))
    has_named = any(os.path.basename(p) != "tiktok_cookies.txt" for p in candidates)
    scored: list[tuple[int, str]] = []
    for p in candidates:
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if size < 1024:
            continue
        if has_named and os.path.basename(p) == "tiktok_cookies.txt":
            continue
        scored.append((size, p))
    if not scored:
        return ""
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][1]


cookies_path = os.getenv("TIKTOK_COOKIES_FILE", "").strip() or _discover_cookies_path()
if not cookies_path or not os.path.isfile(cookies_path):
    print("no cookie file found — drop credentials/tiktok/tiktok_<username>.txt (>= 1 KB)", file=sys.stderr)
    sys.exit(2)
print(f"using cookies from {cookies_path}")

cookies={}
with open(cookies_path) as f:
    for line in f:
        line=line.strip()
        if line.startswith('#') or not line: continue
        p=line.split('\t')
        if len(p)>=7: cookies[p[5]]=p[6]
ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
sec='MS4wLjABAAAAleJo2YXvKa8IPw21h0Lcuu91iCOrJEzerMtDEJdT0LQyCCoyQiUy-uIUBL9cm4rO'
mstok=cookies.get('msToken','')
print('msToken len:', len(mstok))

params={
    'WebIdLastTime': 1700000000,
    'aid': 1988,
    'app_language':'en',
    'app_name':'tiktok_web',
    'browser_language':'en-US',
    'browser_name':'Mozilla',
    'browser_online':'true',
    'browser_platform':'Win32',
    'browser_version':'5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'channel':'tiktok_web',
    'cookie_enabled':'true',
    'count':30,
    'data_collection_enabled':'true',
    'device_id':'7300000000000000000',
    'device_platform':'web_pc',
    'focus_state':'true',
    'from_page':'user',
    'history_len':2,
    'is_fullscreen':'false',
    'is_page_visible':'true',
    'maxCursor':0,
    'minCursor':0,
    'odinId': cookies.get('odin_tt',''),
    'os':'windows',
    'priority_region':'',
    'referer':'',
    'region':'US',
    'scene':21,
    'screen_height':1080,
    'screen_width':1920,
    'secUid': sec,
    'tz_name':'America/Los_Angeles',
    'user_is_login':'true',
    'webcast_language':'en',
    'msToken': mstok,
}
url='https://www.tiktok.com/api/user/list/'
r=httpx.get(url, params=params, cookies=cookies, headers={
    'User-Agent':ua,
    'Referer':'https://www.tiktok.com/',
    'Accept':'application/json, text/plain, */*',
    'Accept-Language':'en-US,en;q=0.9',
}, timeout=25, follow_redirects=True)
print(r.status_code, 'len',len(r.text))
import json
d=r.json()
ul=d.get('userList',[])
print('userList count',len(ul),'total',d.get('total'),'hasMore',d.get('hasMore'),'maxCursor',d.get('maxCursor'),'minCursor',d.get('minCursor'))
for u in ul[:5]:
    user=u.get('user',{})
    print('  @',user.get('uniqueId'),'-',user.get('nickname'),'sec',user.get('secUid','')[:20])
