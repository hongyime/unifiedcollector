"""Debug the live lemon8 tab: what DOM/media does the scraper actually see?

Attaches to the lemon8 tab and evaluates the same helpers the extension
uses (well, an approximation) to see what candidates ARE available. If
the __NEXT_DATA__ has content but our scraper misses it, that's a
selector bug. If __NEXT_DATA__ is empty and the DOM has no card images,
the page is broken (login gate / bot detection).
"""
from __future__ import annotations

import json
import sys
import urllib.request

import websocket  # type: ignore


def main() -> int:
    tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/list").read())
    lemon = next((t for t in tabs if "lemon8" in t.get("url", "") and t.get("type") == "page"), None)
    if not lemon:
        print("no lemon8 tab")
        return 2
    ws = websocket.create_connection(lemon["webSocketDebuggerUrl"])
    n = [0]

    def call(method: str, params: dict | None = None):
        n[0] += 1
        p = {"id": n[0], "method": method}
        if params is not None:
            p["params"] = params
        ws.send(json.dumps(p))
        while True:
            d = json.loads(ws.recv())
            if d.get("id") == n[0]:
                return d

    call("Runtime.enable")
    expr = r"""
    (() => {
      const out = { url: location.href, title: document.title };
      // 1. __NEXT_DATA__ presence and size
      const nd = document.getElementById('__NEXT_DATA__');
      if (nd && nd.textContent) {
        try {
          const parsed = JSON.parse(nd.textContent);
          out.next_data_size = nd.textContent.length;
          const kids = parsed && parsed.props && parsed.props.pageProps ? Object.keys(parsed.props.pageProps).slice(0, 20) : [];
          out.next_data_pageProps_keys = kids;
          // Deep-scan for anything with `image_url` / `note_id` / `title`
          let notes = 0, users = 0, images = 0;
          const stack = [parsed];
          while (stack.length && notes < 200) {
            const x = stack.pop();
            if (!x || typeof x !== 'object') continue;
            if (Array.isArray(x)) { for (const y of x) stack.push(y); continue; }
            const keys = Object.keys(x);
            if (keys.includes('note_id') || keys.includes('item_id')) notes++;
            if (keys.includes('handle') || keys.includes('nickname')) users++;
            for (const k of keys) {
              const v = x[k];
              if (typeof v === 'string' && /https?:\/\/[^"]*(byteimg|ibytedtos|muscdn|lemon8|tos-[a-z]+-[a-z]+)/i.test(v)) images++;
              if (v && typeof v === 'object') stack.push(v);
            }
          }
          out.next_data_notes = notes;
          out.next_data_users = users;
          out.next_data_image_urls = images;
        } catch (e) { out.next_data_err = String(e).slice(0, 200); }
      } else {
        out.next_data_present = false;
      }
      // 2. DOM card images
      const feed = document.querySelector('[data-index], main, [class*="feed"], [class*="card"]');
      const imgs = [...document.querySelectorAll('img[src]')].map(i => i.src).filter(u => /byteimg|ibytedtos|muscdn|lemon8|tos-/i.test(u));
      out.dom_img_matches = imgs.length;
      out.dom_img_sample = imgs.slice(0, 5);
      // 3. Article/card element count
      out.card_count_common_selectors = {
        'article': document.querySelectorAll('article').length,
        'a[href*="/post/"]': document.querySelectorAll('a[href*="/post/"]').length,
        'a[href*="/@"]': document.querySelectorAll('a[href*="/@"]').length,
        'a[href^="/@"]': document.querySelectorAll('a[href^="/@"]').length,
        'div[class*="feed"]': document.querySelectorAll('div[class*="feed"]').length,
        'div[class*="card"]': document.querySelectorAll('div[class*="card"]').length,
      };
      // 4. Body text sample
      out.body_text_first_400 = (document.body ? document.body.innerText : '').slice(0, 400);
      // 5. Login gate detection
      out.has_login_button = !!document.querySelector('[data-testid*="login"], a[href*="/login"], a[href*="/sign-in"]');
      return out;
    })()
    """
    r = call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    val = r.get("result", {}).get("result", {}).get("value")
    print(json.dumps(val, indent=2))
    ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
