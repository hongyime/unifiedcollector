// UnifiedCollector Social Bridge — content script.
//
// Runs ON a social site, so its fetches to that site's internal API are
// SAME-ORIGIN and carry your logged-in session cookies + a real browser
// fingerprint — which bypasses the bot throttles a headless/raw client hits.
// For sites that sign their requests (TikTok/Lemon8), we instead read the
// page's OWN embedded state JSON (already fetched by the page) — also ban-safe.
//
// MULTI-PLATFORM: scrapers live in the PLATFORMS registry keyed by hostname.
// Add a platform by dropping another entry with a `host` matcher and an async
// `runCycle()` returning {targets, saved, discovered}. Remember to also add its
// host to manifest content_scripts + host_permissions + platforms.js, and a
// matching ingest endpoint (the generic /social/* endpoints already cover it).

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitter = (base) => base + Math.random() * base;

// ---------------------------------------------------------------------------
// HUMAN PACING. A real person browsing is slow, irregular, and takes breaks.
// Scraping 257 profiles back-to-back is what got the IG account flagged for
// review. `human(base)` returns base×(0.6–1.6) and ~12% of the time adds a 4–13s
// "distraction" pause; small chance of a long 30–90s coffee break. Use hsleep()
// everywhere instead of fixed sleeps, and keep per-cycle VOLUME small.
function human(base) {
  let ms = base * (0.6 + Math.random());            // 0.6×–1.6×
  if (Math.random() < 0.12) ms += 4000 + Math.random() * 9000;   // distraction
  if (Math.random() < 0.03) ms += 30000 + Math.random() * 60000; // coffee break
  return Math.round(ms);
}
const hsleep = (base) => sleep(human(base));
function shuffle(a) { for (let i = a.length - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; [a[i], a[j]] = [a[j], a[i]]; } return a; }

// ---------------------------------------------------------------------------
// messaging helper — retries once if the ephemeral SW tore down the channel
// (the classic MV3 "message channel closed before a response" race).
// ---------------------------------------------------------------------------
async function send(msg, { retries = 1 } = {}) {
  for (let i = 0; ; i++) {
    try {
      return await chrome.runtime.sendMessage(msg);
    } catch (e) {
      const transient = /message channel closed|Could not establish|Receiving end does not exist|Extension context invalidated/i.test(
        e.message || ""
      );
      if (!transient || i >= retries) {
        if (/Extension context invalidated/i.test(e.message || "")) return null; // page outlived extension
        throw e;
      }
      await sleep(400); // let the worker respawn
    }
  }
}
function clog(level, msg, platform) {
  send({ type: "log", level, msg, platform }).catch(() => {});
}

// A login-wall / throttle returns an HTML doc with HTTP 200. Detect it so we can
// back off cleanly instead of crashing every target with "Unexpected token '<'".
class WallError extends Error {}
async function fetchJson(url, opts) {
  const res = await fetch(url, opts);
  const ctype = res.headers.get("content-type") || "";
  if (!res.ok) {
    if (res.status === 429 || res.status === 401 || res.status === 403) throw new WallError("HTTP " + res.status);
    throw new Error("HTTP " + res.status);
  }
  if (!/json/i.test(ctype)) {
    const head = (await res.text()).slice(0, 40).replace(/\s+/g, " ");
    if (/^<|doctype|<html/i.test(head)) throw new WallError("login/throttle wall");
    throw new Error("non-JSON response");
  }
  return res.json();
}

// Shared collector — dedups media items by content_id+url.
function makeSink() {
  const seen = new Set();
  const items = [];
  return {
    items,
    add(it) {
      if (!it || !it.url || !it.content_id) return;
      const k = it.content_id + "|" + it.url;
      if (seen.has(k)) return;
      seen.add(k);
      items.push(it);
    },
  };
}

// Walk an arbitrary embedded-state object collecting {url, type, id}. Used for
// TikTok/Lemon8 where the page ships its data as JSON in a <script> tag.
function deepCollectMedia(obj, sink, entity, depth = 0) {
  if (!obj || depth > 8) return;
  if (Array.isArray(obj)) {
    for (const v of obj) deepCollectMedia(v, sink, entity, depth + 1);
    return;
  }
  if (typeof obj !== "object") return;
  // video node (tiktok)
  const vid = obj.video || obj.Video;
  if (vid && (vid.playAddr || vid.downloadAddr || vid.PlayAddr)) {
    const url = vid.downloadAddr || vid.playAddr || vid.PlayAddr;
    if (typeof url === "string") sink.add({ content_id: String(obj.id || obj.awemeId || url), content_type: "video", url, entity_name: entity });
  }
  // image post (tiktok photo mode / lemon8)
  const imgs = (obj.imagePost && obj.imagePost.images) || obj.images || obj.imageList || obj.imageInfo;
  if (Array.isArray(imgs)) {
    imgs.forEach((im, i) => {
      const u =
        (im.imageURL && (im.imageURL.urlList || [])[0]) ||
        (im.urlList || [])[0] ||
        im.url || im.imageUrl || (im.imageInfos && im.imageInfos[0] && im.imageInfos[0].url);
      if (typeof u === "string") sink.add({ content_id: String(obj.id || obj.postId || u) + "_" + i, content_type: "photo", url: u, entity_name: entity });
    });
  }
  for (const k in obj) {
    const v = obj[k];
    if (v && typeof v === "object") deepCollectMedia(v, sink, entity, depth + 1);
  }
}

function parseEmbeddedState(ids) {
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el && el.textContent) {
      try { return JSON.parse(el.textContent); } catch (e) {}
    }
  }
  return null;
}

async function autoScroll(times = 8, dist = 1400, pause = 1800) {
  for (let i = 0; i < times; i++) {
    window.scrollBy(0, dist * (0.7 + Math.random() * 0.6));
    await hsleep(pause); // human, irregular scroll cadence
  }
}

// ===========================================================================
// Instagram (same-origin API; full media + 2-hop spider)
// ===========================================================================
const IG_APP_ID = "936619743392459";
const SPIDER_FAMOUS_CAP = 100000;
const SPIDER_FOLLOWS_PER_SIDE = 70;     // was 150 — fewer graph calls per profile
const IG_MAX_ITEMS = 180;               // cap media pages per profile
// Per-cycle target budget: a human checks a HANDFUL of profiles, not 257.
// Randomised each cycle; the rest are picked up on later cycles (round-robin).
function igTargetBudget() { return 4 + ((Math.random() * 5) | 0); } // 4–8 (recovery-week conservative)

const instagram = {
  id: "instagram", host: "www.instagram.com", label: "Instagram",
  csrf() { const m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ""; },
  headers() { return { "x-ig-app-id": IG_APP_ID, "x-csrftoken": this.csrf(), "x-requested-with": "XMLHttpRequest" }; },

  async getProfile(username) {
    const url = "https://www.instagram.com/api/v1/users/web_profile_info/?username=" + encodeURIComponent(username);
    const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
    return j && j.data && j.data.user;
  },
  extractMedia(node, username) {
    const out = [];
    const push = (n, cid) => {
      let url = null, type = "photo";
      if (n.video_url) { url = n.video_url; type = "video"; }
      else if (n.video_versions && n.video_versions[0]) { url = n.video_versions[0].url; type = "video"; }
      else if (n.display_url) { url = n.display_url; }
      else if (n.image_versions2 && n.image_versions2.candidates && n.image_versions2.candidates[0]) { url = n.image_versions2.candidates[0].url; }
      if (url) out.push({ content_id: String(cid), content_type: type, url, entity_name: username });
    };
    const cid = node.id || node.pk || node.code;
    const children = (node.edge_sidecar_to_children && node.edge_sidecar_to_children.edges) || node.carousel_media;
    if (children && children.length) children.forEach((c, i) => push(c.node || c, cid + "_" + i));
    else push(node, cid);
    return out;
  },
  async scrapeUserMedia(user, username, maxItems = IG_MAX_ITEMS) {
    const media = [];
    const tl = user.edge_owner_to_timeline_media;
    if (tl && tl.edges) tl.edges.forEach((e) => media.push(...this.extractMedia(e.node, username)));
    let maxId = tl && tl.page_info && tl.page_info.end_cursor;
    let hasNext = tl && tl.page_info && tl.page_info.has_next_page;
    while (hasNext && media.length < maxItems) {
      await hsleep(4000);
      const url = "https://www.instagram.com/api/v1/feed/user/" + user.id + "/?count=33" + (maxId ? "&max_id=" + maxId : "");
      const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
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
      const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
      (j.users || []).forEach((u) => { if (u && u.username) out.push({ username: u.username }); });
      maxId = j.next_max_id;
      if (!maxId) break;
      await hsleep(4000);
    }
    return out.slice(0, max);
  },
  async runCycle() {
    let resp = [];
    try { resp = (await send({ type: "getTargets", platform: "instagram" })) || []; } catch (e) {}
    let pool = (Array.isArray(resp) ? resp : []).map((t) => (typeof t === "string" ? { username: t, hop: 0 } : t));
    // Only visit a small, RANDOM handful this cycle — like a person checking a few
    // profiles. The rest get picked up on later cycles (server round-robins them).
    const budget = igTargetBudget();
    const targets = shuffle(pool).slice(0, budget);
    const MAX_HOP = 2;
    let saved = 0, discovered = 0, visited = 0;
    clog("info", `cycle start: visiting ${targets.length} of ${pool.length} target(s)`, "instagram");
    for (const t of targets) {
      const username = t.username, hop = typeof t.hop === "number" ? t.hop : 0;
      if (!username) continue;
      try {
        const user = await this.getProfile(username);
        visited++;
        if (!user) continue;
        const fc = (user.edge_followed_by && user.edge_followed_by.count) || 0;
        if (fc > SPIDER_FAMOUS_CAP) { clog("info", `skip famous ${username} (${fc})`, "instagram"); continue; }
        const media = await this.scrapeUserMedia(user, username);
        if (media.length) { await send({ type: "ingest", platform: "instagram", username, items: media }); saved += media.length; }
        // Only crawl the follow-graph SOMETIMES (≈45%) — constant graph crawling is
        // a strong bot signal. Skipping it most visits looks far more human.
        if (hop < MAX_HOP && user.id && Math.random() < 0.3) {
          await hsleep(5000);
          const a = await this.getFollows(user.id, "followers", SPIDER_FOLLOWS_PER_SIDE);
          const b = await this.getFollows(user.id, "following", SPIDER_FOLLOWS_PER_SIDE);
          const found = a.concat(b);
          if (found.length) { await send({ type: "discover", platform: "instagram", source: username, hop, discovered: found }); discovered += found.length; }
        }
      } catch (e) {
        if (e instanceof WallError) {
          clog("warn", `throttled at ${username} — backing off, ending cycle early`, "instagram");
          await send({ type: "wall", platform: "instagram" }).catch(() => {});
          break; // stop hammering; the cooldown lets the session recover
        }
        clog("warn", `scrape failed ${username}: ${e.message}`, "instagram");
      }
      await hsleep(22000); // ~13–35s between profiles, with occasional longer breaks
    }
    return { targets: visited, saved, discovered };
  },
};

// ===========================================================================
// TikTok — read the page's embedded state (SIGI_STATE / __UNIVERSAL_DATA__).
// Scroll to load more posts into that state, then harvest. No request signing.
// NOTE: video CDN URLs are short-lived/cookie-bound; covers + photo-posts are the
// reliable wins. Open a profile or your "Following" feed and leave it.
// ===========================================================================
const tiktok = {
  id: "tiktok", host: "www.tiktok.com", label: "TikTok",
  entity() { const m = location.pathname.match(/^\/@([^/?#]+)/); return m ? m[1] : "feed"; },
  async runCycle() {
    const entity = this.entity();
    clog("info", `cycle start on @${entity}`, "tiktok");
    const sink = makeSink();
    await autoScroll(10);
    const state = parseEmbeddedState(["__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE", "sigi-persisted-data"]);
    if (state) deepCollectMedia(state, sink, entity);
    // also harvest whatever the DOM rendered (posters/sources already loaded)
    document.querySelectorAll("video").forEach((v, i) => {
      const u = v.src || (v.querySelector("source") && v.querySelector("source").src);
      if (u && /^https?:/.test(u)) sink.add({ content_id: "dom_" + i + "_" + u.slice(-24), content_type: "video", url: u, entity_name: entity });
    });
    if (sink.items.length) await send({ type: "ingest", platform: "tiktok", username: entity, items: sink.items });
    return { targets: 1, saved: sink.items.length, discovered: 0 };
  },
};

// ===========================================================================
// Lemon8 — Next.js app: data lives in __NEXT_DATA__ + lazy-loaded into DOM.
// Photo-first platform, so image URLs download cleanly server-side.
// ===========================================================================
const lemon8 = {
  id: "lemon8", host: "www.lemon8-app.com", label: "Lemon8",
  entity() { const m = location.pathname.match(/\/@?([^/?#]+)/); return m ? m[1] : "feed"; },
  async runCycle() {
    const entity = this.entity();
    clog("info", `cycle start on ${entity}`, "lemon8");
    const sink = makeSink();
    await autoScroll(10);
    const state = parseEmbeddedState(["__NEXT_DATA__"]);
    if (state) deepCollectMedia(state, sink, entity);
    document.querySelectorAll("img").forEach((im, i) => {
      const u = im.currentSrc || im.src;
      if (u && /\.(jpe?g|png|webp)/i.test(u) && /https?:/.test(u) && !/icon|avatar|emoji/i.test(u))
        sink.add({ content_id: "img_" + i + "_" + u.slice(-24), content_type: "photo", url: u, entity_name: entity });
    });
    if (sink.items.length) await send({ type: "ingest", platform: "lemon8", username: entity, items: sink.items });
    return { targets: 1, saved: sink.items.length, discovered: 0 };
  },
};

// ===========================================================================
// Twitter / X — SPA with no static state dump; harvest rendered media from the
// timeline DOM (pbs.twimg.com images + video posters). Open Home / a profile's
// Media tab and leave it; scroll loads more.
// ===========================================================================
const x = {
  id: "x", host: "x.com", label: "Twitter / X",
  entity() { const m = location.pathname.match(/^\/([^/?#]+)/); return m && !/^(home|explore|notifications|messages|i|search)$/.test(m[1]) ? m[1] : "timeline"; },
  async runCycle() {
    const entity = this.entity();
    clog("info", `cycle start on ${entity}`, "x");
    const sink = makeSink();
    await autoScroll(12);
    document.querySelectorAll('img[src*="pbs.twimg.com/media"]').forEach((im) => {
      // strip size params → request the original
      let u = im.src.replace(/&name=\w+/, "&name=orig").replace(/\?format=/, "?format=");
      sink.add({ content_id: "img_" + u.split("/media/")[1], content_type: "photo", url: u, entity_name: entity });
    });
    document.querySelectorAll("video").forEach((v, i) => {
      const poster = v.poster;
      if (poster && /https?:/.test(poster)) sink.add({ content_id: "poster_" + i + "_" + poster.slice(-24), content_type: "photo", url: poster, entity_name: entity });
      const u = v.src || (v.querySelector("source") && v.querySelector("source").src);
      if (u && /^https?:/.test(u) && !u.startsWith("blob:")) sink.add({ content_id: "vid_" + i + "_" + u.slice(-24), content_type: "video", url: u, entity_name: entity });
    });
    if (sink.items.length) await send({ type: "ingest", platform: "x", username: entity, items: sink.items });
    return { targets: 1, saved: sink.items.length, discovered: 0 };
  },
};

// ===========================================================================
// Registry + dispatch
// ===========================================================================
const PLATFORMS = [instagram, tiktok, lemon8, x];

function currentPlatform() {
  return PLATFORMS.find((p) => location.hostname === p.host || location.hostname.endsWith("." + p.host)) || null;
}

// ===========================================================================
// CONTINUOUS LOOP (not "cycles"). The work lives HERE in the content script,
// which can run as long as the tab is open — unlike the MV3 service worker,
// which Chrome kills after ~30s idle. So there is no 30-min timer: we just loop
// forever, human-paced (rate-limited + jittered), doing one small pass at a time
// with long rests between. If the loop ever dies (page reload / crash) it is
// respawned: the content script auto-starts it on load, and a lightweight
// service-worker watchdog re-nudges any open tab that isn't looping.
// ===========================================================================
let LOOP_RUNNING = false;

// Rest between passes — a person doesn't scrape non-stop. Tunable.
const PASS_REST_MS = 90000; // ~54s–144s + occasional longer breaks via human()

async function mainLoop() {
  const p = currentPlatform();
  if (!p) { clog("warn", `no scraper for ${location.hostname}`); return; }
  if (LOOP_RUNNING) return;            // one loop per tab
  LOOP_RUNNING = true;
  clog("info", `${p.label} loop started — continuous & human-paced (no fixed timer)`, p.label);
  await send({ type: "loopStatus", platform: p.label, running: true }).catch(() => {});
  try {
    while (LOOP_RUNNING) {
      try {
        const stats = await p.runCycle();  // one pass: IG = a few profiles; others = scrape current page
        await send({ type: "cycleReport", platform: p.label, ...stats }).catch(() => {});
      } catch (e) {
        if (e instanceof WallError) {
          const mins = 40 + Math.floor(Math.random() * 20); // 40–60m
          clog("warn", `${p.label} hit a throttle/login wall — sleeping ${mins}m before resuming`, p.label);
          await send({ type: "wall", platform: p.label, mins }).catch(() => {});
          await sleep(mins * 60000);
          continue;
        }
        clog("error", `${p.label} loop error: ${e.message}`, p.label);
        await sleep(human(60000));
      }
      // heartbeat so the popup shows the loop is alive between passes
      await send({ type: "loopStatus", platform: p.label, running: true }).catch(() => {});
      await sleep(human(PASS_REST_MS)); // long human rest between passes
    }
  } finally {
    LOOP_RUNNING = false;
    await send({ type: "loopStatus", platform: p.label, running: false }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  // "ensureLoop" (watchdog / manual Scrape-now): start the loop if it isn't running.
  if (msg.type === "ensureLoop" || msg.type === "scrapeCycle") {
    sendResponse({ ok: true, running: LOOP_RUNNING });
    if (!LOOP_RUNNING) mainLoop();
    return false;
  }
});

// Auto-start the loop the moment the tab loads (respawns after a reload/crash).
send({ type: "tabReady", platform: (currentPlatform() || {}).id }).catch(() => {});
mainLoop();
