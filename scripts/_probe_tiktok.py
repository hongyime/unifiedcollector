import httpx, urllib.parse
cookies={}
with open('/app/credentials/tiktok/tiktok_cookies.txt') as f:
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
