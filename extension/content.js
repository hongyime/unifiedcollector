// UnifiedCollector Social Bridge — content script.
//
// Runs ON a social site, so its fetches to that site's internal API are
// SAME-ORIGIN and carry your logged-in session cookies + a real browser
// fingerprint — which bypasses the bot throttles a headless/raw client hits.
//
// MULTI-PLATFORM: scrapers live in the PLATFORMS registry keyed by hostname.
// Instagram is implemented; add a new platform by dropping another entry with a
// `host` matcher and an async `runCycle()` that returns {targets, saved, discovered}.
// (Remember to also add its host to manifest content_scripts + host_permissions,
//  and a matching ingest endpoint on the collector side.)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitter = (base) => base + Math.random() * base;

// forward a log line to the background worker so it shows in the popup's Log panel
function clog(level, msg, platform) {
  try { chrome.runtime.sendMessage({ type: "log", level, msg, platform }); } catch (e) {}
}

// ===========================================================================
// Instagram platform
// ===========================================================================
const IG_APP_ID = "936619743392459";
const SPIDER_FAMOUS_CAP = 100000;     // skip celebrities — we want your network
const SPIDER_FOLLOWS_PER_SIDE = 150;

const instagram = {
  host: "www.instagram.com",
  label: "Instagram",

  csrf() { const m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ""; },
  headers() {
    return { "x-ig-app-id": IG_APP_ID, "x-csrftoken": this.csrf(), "x-requested-with": "XMLHttpRequest" };
  },

  async getProfile(username) {
    const url = "https://www.instagram.com/api/v1/users/web_profile_info/?username=" + encodeURIComponent(username);
    const res = await fetch(url, { headers: this.headers(), credentials: "include" });
    if (!res.ok) throw new Error("web_profile_info " + res.status);
    const j = await res.json();
    return j && j.data && j.data.user;
  },

  extractMedia(node, username) {
    const out = [];
    const push = (n, cid) => {
      let url = null, type = "photo";
      if (n.video_url) { url = n.video_url; type = "video"; }
      else if (n.video_versions && n.video_versions[0]) { url = n.video_versions[0].url; type = "video"; }
      else if (n.display_url) { url = n.display_url; }
      else if (n.image_versions2 && n.image_versions2.candidates && n.image_versions2.candidates[0]) {
        url = n.image_versions2.candidates[0].url;
      }
      if (url) out.push({ content_id: String(cid), content_type: type, url, entity_name: username });
    };
    const cid = node.id || node.pk || node.code;
    const children = (node.edge_sidecar_to_children && node.edge_sidecar_to_children.edges) || node.carousel_media;
    if (children && children.length) children.forEach((c, i) => push(c.node || c, cid + "_" + i));
    else push(node, cid);
    return out;
  },

  async scrapeUserMedia(user, username, maxItems = 300) {
    const media = [];
    const tl = user.edge_owner_to_timeline_media;
    if (tl && tl.edges) tl.edges.forEach((e) => media.push(...this.extractMedia(e.node, username)));
    let maxId = tl && tl.page_info && tl.page_info.end_cursor;
    let hasNext = tl && tl.page_info && tl.page_info.has_next_page;
    while (hasNext && media.length < maxItems) {
      await sleep(jitter(1500));
      const url = "https://www.instagram.com/api/v1/feed/user/" + user.id + "/?count=33" + (maxId ? "&max_id=" + maxId : "");
      let res;
      try { res = await fetch(url, { headers: this.headers(), credentials: "include" }); } catch (e) { break; }
      if (!res.ok) break;
      const j = await res.json();
      (j.items || []).forEach((it) => media.push(...this.extractMedia(it, username)));
      maxId = j.next_max_id;
      hasNext = j.more_available && !!maxId;
    }
    return media;
  },

  async getFollows(userId, kind, max) {
    const out = [];
    let maxId = "";
    while (out.length < max) {
      const url = "https://www.instagram.com/api/v1/friendships/" + userId + "/" + kind + "/?count=50" + (maxId ? "&max_id=" + maxId : "");
      let res;
      try { res = await fetch(url, { headers: this.headers(), credentials: "include" }); } catch (e) { break; }
      if (!res.ok) break;
      const j = await res.json();
      (j.users || []).forEach((u) => { if (u && u.username) out.push({ username: u.username }); });
      maxId = j.next_max_id;
      if (!maxId) break;
      await sleep(jitter(1500));
    }
    return out.slice(0, max);
  },

  async runCycle() {
    let resp = [];
    try { resp = (await chrome.runtime.sendMessage({ type: "getTargets" })) || []; } catch (e) {}
    const targets = (Array.isArray(resp) ? resp : []).map((t) => (typeof t === "string" ? { username: t, hop: 0 } : t));
    const MAX_HOP = 2;
    let saved = 0, discovered = 0;
    clog("info", `cycle start: ${targets.length} target(s)`, "instagram");
    for (const t of targets) {
      const username = t.username, hop = typeof t.hop === "number" ? t.hop : 0;
      if (!username) continue;
      try {
        const user = await this.getProfile(username);
        if (!user) continue;
        const fc = (user.edge_followed_by && user.edge_followed_by.count) || 0;
        if (fc > SPIDER_FAMOUS_CAP) { clog("info", `skip famous ${username} (${fc})`, "instagram"); continue; }
        const media = await this.scrapeUserMedia(user, username);
        if (media.length) {
          await chrome.runtime.sendMessage({ type: "ingest", username, items: media });
          saved += media.length;
        }
        if (hop < MAX_HOP && user.id) {
          const a = await this.getFollows(user.id, "followers", SPIDER_FOLLOWS_PER_SIDE);
          const b = await this.getFollows(user.id, "following", SPIDER_FOLLOWS_PER_SIDE);
          const found = a.concat(b);
          if (found.length) {
            await chrome.runtime.sendMessage({ type: "discover", source: username, hop, discovered: found });
            discovered += found.length;
          }
        }
      } catch (e) {
        clog("warn", `scrape failed ${username}: ${e.message}`, "instagram");
      }
      await sleep(jitter(4000));
    }
    return { targets: targets.length, saved, discovered };
  },
};

// ===========================================================================
// Registry + dispatch
// ===========================================================================
const PLATFORMS = [instagram /* , tiktok, twitter, ... */];

function currentPlatform() {
  return PLATFORMS.find((p) => location.hostname === p.host || location.hostname.endsWith("." + p.host)) || null;
}

async function runCycle() {
  const p = currentPlatform();
  if (!p) { clog("warn", `no scraper for ${location.hostname}`); return; }
  try {
    const stats = await p.runCycle();
    chrome.runtime.sendMessage({ type: "cycleReport", platform: p.label, ...stats });
  } catch (e) {
    clog("error", `${p.label} cycle error: ${e.message}`, p.label);
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "scrapeCycle") {
    runCycle().then(() => sendResponse({ ok: true }));
    return true;
  }
});
