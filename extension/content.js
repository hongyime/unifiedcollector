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

// Per-origin persisted state. Our following/foryou rotation + profile-visit queue
// need to survive the page reloads that navigation causes, so we stash counters in
// localStorage (scoped to the platform's origin, so no cross-talk between sites).
const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };
const lsNum = (k) => { const n = parseInt(lsGet(k, "0"), 10); return Number.isFinite(n) ? n : 0; };
const lsBump = (k) => { const n = lsNum(k) + 1; lsSet(k, String(n)); return n; };

// PERSISTENT throttle wall (anti-ban). When Instagram throttles/challenges us, the
// per-loop 45m sleep used to die on every tab refresh / SW wake / reload — so a fresh
// loop immediately re-hit a *flagged* account every ~1-2 min (the worst thing for a
// ban). Persist the wall expiry in localStorage so ANY respawned loop honours it and
// leaves IG completely alone until it clears. Each new wall extends the rest.
function wallLeftMs(platform) { return Math.max(0, lsNum("uc_wall_" + platform) - Date.now()); }
function setWall(platform, mins) { lsSet("uc_wall_" + platform, String(Date.now() + mins * 60000)); }

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

// Capture is ALWAYS ON (user: "i want them on at all times"). Stories, highlights
// and comments are captured every cycle — no toggles. Pacing is handled by the
// human-paced loop + wall cooldown, not by disabling capture.
const CAPTURE = { stories: true, highlights: true, comments: true };

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

// Stable id from a media URL (origin+path, volatile query stripped) so the SAME
// image gets the SAME content_id across re-scrapes -> server dedups before
// downloading. Fixes the lemon8/tiktok re-download duplication.
function urlId(u) {
  let s = u || "";
  try { const x = new URL(u); s = x.origin + x.pathname; } catch (e) {}
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(36);
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

// Scan embedded state for user objects (TikTok uniqueId/nickname, Lemon8 handles)
// so we record every creator/commenter we come across into social_users.
function deepCollectUsers(obj, users, depth = 0) {
  if (!obj || depth > 8 || users.length > 800) return;
  if (Array.isArray(obj)) { for (const v of obj) deepCollectUsers(v, users, depth + 1); return; }
  if (typeof obj !== "object") return;
  const uname = obj.uniqueId || obj.unique_id || obj.handle || (obj.author && obj.author.uniqueId);
  if (typeof uname === "string" && uname && uname.length < 40) {
    users.push({
      user_id: obj.id || obj.uid || obj.userId || null, username: uname,
      display_name: obj.nickname || obj.nick_name || obj.name || null,
      profile_pic_url: obj.avatarThumb || obj.avatar_thumb || obj.avatarMedium || obj.avatar || null,
    });
  }
  for (const k in obj) { const v = obj[k]; if (v && typeof v === "object") deepCollectUsers(v, users, depth + 1); }
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
const SPIDER_FAMOUS_CAP = 3000;   // skip accounts > 3k followers (focus on close network)
const SPIDER_FOLLOWS_PER_SIDE = 70;     // was 150 — fewer graph calls per profile
const IG_MAX_ITEMS = 180;               // cap media pages per profile
// Per-cycle target budget: a human checks a HANDFUL of profiles, not 257.
// Randomised each cycle; the rest are picked up on later cycles (round-robin).
function igTargetBudget() { return 4 + ((Math.random() * 5) | 0); } // 4–8 deep profiles/cycle
const IG_STORY_SWEEP = 10;   // profiles to grab EXPIRING stories/highlights from, first, each cycle
const IG_SEEDED_ACCOUNTS = new Set();  // ds_user_ids self-seeded this session (re-seeds on account switch)

const instagram = {
  id: "instagram", host: "www.instagram.com", label: "Instagram",
  csrf() { const m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ""; },
  headers() { return { "x-ig-app-id": IG_APP_ID, "x-csrftoken": this.csrf(), "x-requested-with": "XMLHttpRequest" }; },

  async getProfile(username) {
    const url = "https://www.instagram.com/api/v1/users/web_profile_info/?username=" + encodeURIComponent(username);
    const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
    return j && j.data && j.data.user;
  },
  // Push the full profile (bio, counts, profile photo) -> instagram_profiles +
  // social_users; the server also downloads the profile pic as kind=profile.
  async _sendProfile(user) {
    if (!user || !user.username) return;
    await send({ type: "profile", platform: "instagram", profile: {
      user_id: user.id, username: user.username, full_name: user.full_name,
      bio: user.biography,
      followers_count: user.edge_followed_by && user.edge_followed_by.count,
      following_count: user.edge_follow && user.edge_follow.count,
      posts_count: user.edge_owner_to_timeline_media && user.edge_owner_to_timeline_media.count,
      is_verified: user.is_verified, is_private: user.is_private,
      profile_pic_url: user.profile_pic_url_hd || user.profile_pic_url,
      external_url: user.external_url,
    } }).catch(() => {});
  },
  // Pull caption + engagement off a post node — these are ALREADY in the
  // web_profile_info / feed/user response, so capturing them costs no extra
  // requests. Handles both the GraphQL (edge_*) and v1 (caption/like_count) shapes.
  postMeta(n) {
    const caption =
      (n.edge_media_to_caption && n.edge_media_to_caption.edges && n.edge_media_to_caption.edges[0] && n.edge_media_to_caption.edges[0].node.text) ||
      (n.caption && (n.caption.text || (typeof n.caption === "string" ? n.caption : null))) || null;
    const likes = (n.edge_liked_by && n.edge_liked_by.count) ?? (n.edge_media_preview_like && n.edge_media_preview_like.count) ?? n.like_count ?? null;
    const comments = (n.edge_media_to_comment && n.edge_media_to_comment.count) ?? (n.edge_media_to_parent_comment && n.edge_media_to_parent_comment.count) ?? n.comment_count ?? null;
    const views = n.video_view_count ?? n.view_count ?? n.play_count ?? null;
    return {
      caption, likes_count: likes, comments_count: comments, views_count: views,
      taken_at: n.taken_at_timestamp || n.taken_at || null,
      shortcode: n.shortcode || n.code || null,
      location: (n.location && (n.location.name || n.location.short_name)) || null,
      location_lat: (n.location && (n.location.lat ?? n.location.latitude)) ?? null,
      location_lng: (n.location && (n.location.lng ?? n.location.longitude)) ?? null,
      music_title: (n.clips_music_attribution_info && n.clips_music_attribution_info.song_name)
        || (n.music_metadata && n.music_metadata.music_info && n.music_metadata.music_info.music_asset_info && n.music_metadata.music_info.music_asset_info.title) || null,
      music_author: (n.clips_music_attribution_info && n.clips_music_attribution_info.artist_name)
        || (n.music_metadata && n.music_metadata.music_info && n.music_metadata.music_info.music_asset_info && n.music_metadata.music_info.music_asset_info.display_artist) || null,
    };
  },

  extractMedia(node, username) {
    const out = [];
    const meta = this.postMeta(node);
    const push = (n, cid) => {
      let url = null, type = "photo";
      if (n.video_url) { url = n.video_url; type = "video"; }
      else if (n.video_versions && n.video_versions[0]) { url = n.video_versions[0].url; type = "video"; }
      else if (n.display_url) { url = n.display_url; }
      else if (n.image_versions2 && n.image_versions2.candidates && n.image_versions2.candidates[0]) { url = n.image_versions2.candidates[0].url; }
      if (url) out.push({ content_id: String(cid), content_type: type, url, entity_name: username, meta });
    };
    const cid = node.id || node.pk || node.code;
    const children = (node.edge_sidecar_to_children && node.edge_sidecar_to_children.edges) || node.carousel_media;
    if (children && children.length) children.forEach((c, i) => push(c.node || c, cid + "_" + i));
    else push(node, cid);
    return out;
  },
  // Build a structured post record (caption/likes/comments/hashtags/location) for
  // the instagram_posts table. Hashtags/mentions parsed from the caption text.
  buildPost(n) {
    const m = this.postMeta(n);
    const cap = m.caption || "";
    return {
      platform_post_id: String(n.id || n.pk || n.code),
      media_type: (n.is_video || n.video_versions) ? "video"
        : (n.carousel_media || n.edge_sidecar_to_children) ? "carousel" : "photo",
      caption: m.caption,
      hashtags: (cap.match(/#[\w.]+/g) || []).map((s) => s.slice(1)),
      mentions: (cap.match(/@[\w.]+/g) || []).map((s) => s.slice(1)),
      location: m.location,
      location_lat: m.location_lat, location_lng: m.location_lng,
      music_title: m.music_title, music_author: m.music_author,
      likes_count: m.likes_count, comments_count: m.comments_count,
      taken_at: m.taken_at, video_duration: n.video_duration || null,
      metadata: { views_count: m.views_count, shortcode: m.shortcode },
    };
  },

  async scrapeUserMedia(user, username, maxItems = IG_MAX_ITEMS) {
    const media = [], posts = [];
    const tl = user.edge_owner_to_timeline_media;
    if (tl && tl.edges) tl.edges.forEach((e) => { media.push(...this.extractMedia(e.node, username)); posts.push(this.buildPost(e.node)); });
    let maxId = tl && tl.page_info && tl.page_info.end_cursor;
    let hasNext = tl && tl.page_info && tl.page_info.has_next_page;
    while (hasNext && media.length < maxItems) {
      await hsleep(4000);
      const url = "https://www.instagram.com/api/v1/feed/user/" + user.id + "/?count=33" + (maxId ? "&max_id=" + maxId : "");
      const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
      (j.items || []).forEach((it) => { media.push(...this.extractMedia(it, username)); posts.push(this.buildPost(it)); });
      maxId = j.next_max_id;
      hasNext = j.more_available && !!maxId;
    }
    return { media, posts };
  },

  // ---- stories / highlights / comments (opt-in; extra requests) ----------
  storyItemMedia(it, username, kind) {
    let url = null, type = "photo";
    if (it.video_versions && it.video_versions[0]) { url = it.video_versions[0].url; type = "video"; }
    else if (it.image_versions2 && it.image_versions2.candidates && it.image_versions2.candidates[0]) { url = it.image_versions2.candidates[0].url; }
    const cid = it.pk || it.id;
    const taken_at = it.taken_at || it.device_timestamp || null;
    return url ? [{ content_id: String(cid), content_type: type, url, entity_name: username, kind, taken_at }] : [];
  },
  _reelItems(j, key) {
    const reel = (j.reels && j.reels[key]) || (j.reels_media && j.reels_media[0]) || null;
    return (reel && reel.items) || [];
  },
  async getStories(userId, username) {
    const url = "https://www.instagram.com/api/v1/feed/reels_media/?reel_ids=" + encodeURIComponent(userId);
    const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
    const media = [];
    this._reelItems(j, String(userId)).forEach((it) => media.push(...this.storyItemMedia(it, username, "story")));
    return media;
  },
  async getHighlights(userId, username, maxReels = 5) {
    const tray = await fetchJson("https://www.instagram.com/api/v1/highlights/" + userId + "/highlights_tray/", { headers: this.headers(), credentials: "include" });
    const reels = (tray.tray || []).slice(0, maxReels);
    const media = [];
    for (const r of reels) {
      await hsleep(4000);
      const j = await fetchJson("https://www.instagram.com/api/v1/feed/reels_media/?reel_ids=" + encodeURIComponent(r.id), { headers: this.headers(), credentials: "include" });
      this._reelItems(j, String(r.id)).forEach((it) => media.push(...this.storyItemMedia(it, username, "highlight")));
    }
    return media;
  },
  async getComments(mediaId) {
    const url = "https://www.instagram.com/api/v1/media/" + mediaId + "/comments/?can_support_threading=true&permalink_enabled=false";
    const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
    return (j.comments || []).map((c) => ({
      platform_comment_id: String(c.pk || c.id),
      author_username: c.user && c.user.username,
      author_platform_id: c.user && String(c.user.pk),
      text: c.text, like_count: c.comment_like_count,
      created_at: c.created_at, is_reply: false,
    }));
  },

  // Tagged posts: posts where THIS profile is tagged by someone else (their
  // "Tagged" tab). We file the media under the scraped profile (kind=tagged) and
  // record every tagged user + the post owner into the user registry.
  async getTagged(userId, username) {
    const media = [], users = [];
    let maxId = "";
    for (let page = 0; page < 3; page++) {
      const url = "https://www.instagram.com/api/v1/usertags/" + userId + "/feed/?count=24" + (maxId ? "&max_id=" + maxId : "");
      const j = await fetchJson(url, { headers: this.headers(), credentials: "include" });
      (j.items || []).forEach((it) => {
        this.extractMedia(it, username).forEach((m) => media.push({ ...m, kind: "tagged" }));
        if (it.user) users.push({ user_id: it.user.pk, username: it.user.username, display_name: it.user.full_name });
        const tags = (it.usertags && it.usertags.in) || [];
        tags.forEach((t) => { if (t.user) users.push({ user_id: t.user.pk, username: t.user.username, display_name: t.user.full_name }); });
      });
      maxId = j.next_max_id;
      if (!maxId || !j.more_available) break;
      await hsleep(4000);
    }
    return { media, users };
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

  // EXPIRING content (24h stories + highlights) — grab these FIRST each cycle.
  async _expiring(user, username) {
    let saved = 0;
    if (!user.id) return 0;
    await hsleep(3500);
    try { const s = await this.getStories(user.id, username); if (s.length) { await send({ type: "ingest", platform: "instagram", username, items: s }); saved += s.length; clog("info", `${username}: +${s.length} story media (expiring)`, "instagram"); } }
    catch (e) { if (e instanceof WallError) throw e; }
    await hsleep(3500);
    try { const h = await this.getHighlights(user.id, username); if (h.length) { await send({ type: "ingest", platform: "instagram", username, items: h }); saved += h.length; clog("info", `${username}: +${h.length} highlight media`, "instagram"); } }
    catch (e) { if (e instanceof WallError) throw e; }
    return saved;
  },

  // DEEP scrape — timeline media + post metadata + comments + tagged + follow-graph.
  async _deep(user, username, hop) {
    let saved = 0, discovered = 0;
    const { media, posts } = await this.scrapeUserMedia(user, username);
    if (media.length) { await send({ type: "ingest", platform: "instagram", username, items: media }); saved += media.length; }
    if (posts.length) { await send({ type: "posts", platform: "instagram", username, posts }); }
    for (const p of posts.slice(0, 3)) {  // comments: heavy, cap a few posts
      await hsleep(6000);
      try { const cm = await this.getComments(p.platform_post_id); if (cm.length) await send({ type: "comments", platform: "instagram", post_id: p.platform_post_id, comments: cm }); }
      catch (e) { if (e instanceof WallError) throw e; }
    }
    if (user.id) {  // tagged posts -> filed under this profile + tagged users recorded
      await hsleep(5000);
      try {
        const tg = await this.getTagged(user.id, username);
        if (tg.media.length) { await send({ type: "ingest", platform: "instagram", username, items: tg.media }); saved += tg.media.length; clog("info", `${username}: +${tg.media.length} tagged media`, "instagram"); }
        if (tg.users.length) await send({ type: "users", platform: "instagram", context: "tagged", users: tg.users });
      } catch (e) { if (e instanceof WallError) throw e; }
    }
    if (hop < 2 && user.id && Math.random() < 0.3) {  // follow-graph sometimes (human)
      await hsleep(5000);
      const a = await this.getFollows(user.id, "followers", SPIDER_FOLLOWS_PER_SIDE);
      const b = await this.getFollows(user.id, "following", SPIDER_FOLLOWS_PER_SIDE);
      const found = a.concat(b);
      if (found.length) { await send({ type: "discover", platform: "instagram", source: username, hop, discovered: found }); discovered += found.length; }
    }
    return { saved, discovered };
  },

  // Seed the spider from YOUR OWN followers + following (user: "instagram should
  // start from my own followers as seeds"). Runs once per session; the logged-in
  // user id is the ds_user_id cookie. Capped + paced for account safety.
  async seedFromSelf() {
    const m = document.cookie.match(/ds_user_id=(\d+)/);
    if (!m) return;
    const myId = m[1];
    // Owner tag so ig_ingest can record a PER-ACCOUNT follow graph (follow_edges).
    // The extension only ever sees the ONE account you're logged into; you switch
    // accounts (IG's switcher) and this re-runs per account (see runCycle gate).
    let ownerUsername = null;
    try { ownerUsername = (window._sharedData && window._sharedData.config && window._sharedData.config.viewer && window._sharedData.config.viewer.username) || null; } catch (e) {}
    const owner = { id: myId, username: ownerUsername };
    let followers = [], following = [];
    try {
      followers = await this.getFollows(myId, "followers", 200);
      following = await this.getFollows(myId, "following", 200);
    } catch (e) { return; }
    // Record MY real follow graph with the CORRECT relationship context + owner so
    // social_users reflects who follows me ('follower') vs who I follow ('follow'),
    // and follow_edges records it per account. Uses /social/users (honours both).
    if (followers.length) await send({ type: "users", platform: "instagram", context: "follower", owner, users: followers }).catch(() => {});
    if (following.length) await send({ type: "users", platform: "instagram", context: "follow", owner, users: following }).catch(() => {});
    // Still seed the spider from the combined set (hop-0 expansion).
    const users = followers.concat(following);
    if (users.length) {
      await send({ type: "seed", platform: "instagram", users }).catch(() => {});
      clog("info", `self-seed [${ownerUsername || myId}]: ${followers.length} followers + ${following.length} following recorded + seeded`, "instagram");
    }
  },

  async runCycle() {
    // ANTI-BAN (cooperative): the HEADLESS collector shares this IG account. If it's
    // in a 429 cooldown, the extension must rest too — adopt its remaining time as
    // our own wall so we don't probe a flagged account from the other side.
    try {
      const cd = await send({ type: "igCooldown" });
      if (cd && cd.cooling && cd.secs_left > 0) {
        setWall("instagram", Math.ceil(cd.secs_left / 60));
        clog("warn", `IG: headless in 429 cooldown (streak ${cd.streak}, ${Math.ceil(cd.secs_left / 60)}m) — extension resting in sync`, "instagram");
      }
    } catch (e) {}
    // if IG threw a throttle/challenge wall recently (either path), do NOT touch IG
    // at all until it clears — rest in chunks (survives loop respawns via localStorage).
    const left = wallLeftMs("instagram");
    if (left > 0) {
      clog("warn", `IG throttled — resting, ${Math.ceil(left / 60000)}m left (not touching IG)`, "instagram");
      await sleep(Math.min(left, 600000)); // re-check every ≤10m
      return { targets: 0, saved: 0, discovered: 0, walled: true };
    }
    // Per-account self-seed: capture EACH account's own graph as you switch to it.
    // The extension can't auto-switch IG accounts — you switch (IG's account
    // switcher changes the ds_user_id cookie); when a not-yet-seeded account is
    // active this cycle, seed it once. Covers all your accounts by rotating logins.
    const _dsm = document.cookie.match(/ds_user_id=(\d+)/);
    const _curId = _dsm ? _dsm[1] : null;
    if (_curId && !IG_SEEDED_ACCOUNTS.has(_curId)) { IG_SEEDED_ACCOUNTS.add(_curId); this.seedFromSelf(); }
    let resp = [];
    try { resp = (await send({ type: "getTargets", platform: "instagram" })) || []; } catch (e) {}
    const pool = (Array.isArray(resp) ? resp : [])
      .map((t) => (typeof t === "string" ? { username: t, hop: 0 } : t))
      .filter((t) => t && t.username);
    if (!pool.length) return { targets: 0, saved: 0, discovered: 0 };
    const cache = new Map();
    const getUser = async (u) => { if (cache.has(u)) return cache.get(u); const p = await this.getProfile(u); cache.set(u, p); return p; };
    const okProfile = (user) => user && ((user.edge_followed_by && user.edge_followed_by.count) || 0) <= SPIDER_FAMOUS_CAP;
    let saved = 0, discovered = 0, visited = 0;

    // PASS 1 — EXPIRING FIRST. The server orders seeds (hop 0) at the front, so we
    // sweep stories/highlights for your seed profiles + early network every cycle.
    const sweep = pool.slice(0, IG_STORY_SWEEP);
    clog("info", `expiring-first: stories/highlights sweep of ${sweep.length} profile(s)`, "instagram");
    for (const t of sweep) {
      try {
        const user = await getUser(t.username);
        if (!okProfile(user)) continue;
        await this._sendProfile(user);
        saved += await this._expiring(user, t.username);
      } catch (e) {
        if (e instanceof WallError) { setWall("instagram", 45); clog("warn", "throttled in sweep — backing off 45m (persisted, survives refresh)", "instagram"); await send({ type: "wall", platform: "instagram", mins: 45 }).catch(() => {}); return { targets: visited, saved, discovered, walled: true }; }
        clog("warn", `sweep failed ${t.username}: ${e.message}`, "instagram");
      }
      await hsleep(9000);
    }

    // PASS 2 — DEEP scrape a small random handful (timeline + posts + comments + tagged).
    const deep = shuffle(pool.slice()).slice(0, igTargetBudget());
    clog("info", `deep scrape: ${deep.length} profile(s)`, "instagram");
    for (const t of deep) {
      const hop = typeof t.hop === "number" ? t.hop : 0;
      try {
        const user = await getUser(t.username);
        visited++;
        if (!user) continue;
        if (!okProfile(user)) { clog("info", `skip famous ${t.username}`, "instagram"); continue; }
        await this._sendProfile(user);
        const r = await this._deep(user, t.username, hop);
        saved += r.saved; discovered += r.discovered;
      } catch (e) {
        if (e instanceof WallError) { setWall("instagram", 45); clog("warn", `throttled at ${t.username} — backing off 45m (persisted)`, "instagram"); await send({ type: "wall", platform: "instagram", mins: 45 }).catch(() => {}); break; }
        clog("warn", `scrape failed ${t.username}: ${e.message}`, "instagram");
      }
      await hsleep(22000); // ~13–35s between profiles
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
    if (state) { deepCollectMedia(state, sink, entity); const us = []; deepCollectUsers(state, us); if (us.length) await send({ type: "users", platform: "tiktok", context: "seen", users: us }); }
    // also harvest whatever the DOM rendered (posters/sources already loaded)
    document.querySelectorAll("video").forEach((v, i) => {
      const u = v.src || (v.querySelector("source") && v.querySelector("source").src);
      if (u && /^https?:/.test(u)) sink.add({ content_id: "dom_" + urlId(u), content_type: "video", url: u, entity_name: entity });
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
    if (state) { deepCollectMedia(state, sink, entity); const us = []; deepCollectUsers(state, us); if (us.length) await send({ type: "users", platform: "lemon8", context: "seen", users: us }); }
    document.querySelectorAll("img").forEach((im, i) => {
      const u = im.currentSrc || im.src;
      if (u && /\.(jpe?g|png|webp)/i.test(u) && /https?:/.test(u) && !/icon|avatar|emoji/i.test(u))
        sink.add({ content_id: "img_" + urlId(u), content_type: "photo", url: u, entity_name: entity });
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
// Read tweet records straight off the DOM. This is the reliable path for X — it
// does NOT depend on the inject GraphQL hook firing (X throttles background-tab
// fetches and the home feed can load before the hook installs). Each <article> has
// a permalink (author + id), tweetText, and a [role=group] whose aria-label carries
// the full counts ("13 replies, 4 reposts, 88 likes, 9,000 views").
function harvestXPosts(entity) {
  const posts = [];
  document.querySelectorAll('article[data-testid="tweet"], article[role="article"]').forEach((art) => {
    try {
      const link = art.querySelector('a[href*="/status/"]');
      const m = link && (link.getAttribute("href") || "").match(/^\/([A-Za-z0-9_]{1,20})\/status\/(\d+)/);
      if (!m) return;
      const author = m[1], pid = m[2];
      const textEl = art.querySelector('[data-testid="tweetText"]');
      const caption = textEl ? (textEl.innerText || "").trim().slice(0, 2000) : null;
      const grp = art.querySelector('[role="group"][aria-label]');
      const al = grp ? (grp.getAttribute("aria-label") || "") : "";
      const num = (re) => { const x = al.match(re); return x ? parseInt(x[1].replace(/[,.\s]/g, ""), 10) : null; };
      posts.push({
        platform_post_id: pid, author_username: author, caption: caption || null,
        comments_count: num(/([\d,.]+)\s+repl/i), reposts_count: num(/([\d,.]+)\s+repost/i),
        likes_count: num(/([\d,.]+)\s+like/i), views_count: num(/([\d,.]+)\s+view/i),
        media_type: "tweet",
        hashtags: caption ? (caption.match(/#[\w]+/g) || []).map((s) => s.slice(1)) : [],
        mentions: caption ? (caption.match(/@[\w]+/g) || []).map((s) => s.slice(1)) : [],
      });
    } catch (e) {}
  });
  return posts;
}

// Click a named home-timeline tab ("Following" or "For you"). Following is primary;
// For-You runs as the occasional secondary pass (see rotation in runCycle).
async function xSelectTab(name) {
  try {
    const tab = [...document.querySelectorAll('[role="tab"], a[role="tab"]')]
      .find((t) => (t.textContent || "").trim().toLowerCase() === name.toLowerCase());
    if (tab) {
      if (tab.getAttribute("aria-selected") !== "true") { tab.scrollIntoView({ block: "center" }); tab.click(); await sleep(jitter(2500)); }
      return true;
    }
  } catch (e) {}
  return false;
}

const x = {
  id: "x", host: "x.com", label: "Twitter / X",
  entity() { const m = location.pathname.match(/^\/([^/?#]+)/); return m && !/^(home|explore|notifications|messages|i|search)$/.test(m[1]) ? m[1] : "timeline"; },
  async runCycle() {
    const entity = this.entity();
    // following-PRIMARY, for-you SECONDARY: most cycles read Following; every 4th
    // cycle take a For-You pass so we still capture trending/outside-graph tweets.
    let feed = entity;
    if (/^\/home/.test(location.pathname)) {
      const c = lsBump("uc_x_cyc");
      if (c % 4 === 0) feed = (await xSelectTab("For you")) ? "home/foryou" : "home";
      else feed = (await xSelectTab("Following")) ? "home/following" : "home";
    }
    clog("info", `X cycle on ${feed} — scrolling for tweets`, "x");
    const sink = makeSink();
    await autoScroll(12);
    // post engagement (the x_posts gap) — DOM-harvested, hook-independent.
    const xposts = harvestXPosts(feed);
    if (xposts.length) {
      await send({ type: "posts", platform: "x", username: feed, posts: xposts });
      clog("info", `X ${feed}: ${xposts.length} tweet(s) w/ counts`, "x");
    }
    document.querySelectorAll('img[src*="pbs.twimg.com/media"]').forEach((im) => {
      // strip size params → request the original
      let u = im.src.replace(/&name=\w+/, "&name=orig").replace(/\?format=/, "?format=");
      sink.add({ content_id: "img_" + u.split("/media/")[1], content_type: "photo", url: u, entity_name: entity });
    });
    const xu = collectPermalinkAuthors(/^\/([A-Za-z0-9_]{1,20})\/status\//, /^(home|explore|search|messages|notifications|i|settings)$/);
    if (xu.length) await send({ type: "users", platform: "x", context: "seen", users: xu });
    document.querySelectorAll("video").forEach((v, i) => {
      const poster = v.poster;
      if (poster && /https?:/.test(poster)) sink.add({ content_id: "poster_" + urlId(poster), content_type: "photo", url: poster, entity_name: entity });
      const u = v.src || (v.querySelector("source") && v.querySelector("source").src);
      if (u && /^https?:/.test(u) && !u.startsWith("blob:")) sink.add({ content_id: "vid_" + urlId(u), content_type: "video", url: u, entity_name: entity });
    });
    if (sink.items.length) await send({ type: "ingest", platform: "x", username: entity, items: sink.items });
    return { targets: 1, saved: sink.items.length, posts: xposts.length, discovered: xu.length };
  },
};

// ===========================================================================
// Shared DOM media harvester (Threads / Facebook) — read rendered CDN images +
// video posters/sources. Pure DOM reads (no API calls) = very low ban profile.
// The server-side file gate drops avatars/thumbnails/UI chrome by size.
// ===========================================================================
function harvestDom(entity, { imgRe, junkRe }) {
  const sink = makeSink();
  document.querySelectorAll("img").forEach((im) => {
    const u = im.currentSrc || im.src;
    if (u && imgRe.test(u) && !junkRe.test(u))
      sink.add({ content_id: "img_" + urlId(u), content_type: "photo", url: u, entity_name: entity });
  });
  document.querySelectorAll("video").forEach((v) => {
    if (v.poster && /https?:/.test(v.poster) && !junkRe.test(v.poster))
      sink.add({ content_id: "poster_" + urlId(v.poster), content_type: "photo", url: v.poster, entity_name: entity });
    const u = v.src || (v.querySelector("source") && v.querySelector("source").src);
    if (u && /^https?:/.test(u) && !u.startsWith("blob:"))
      sink.add({ content_id: "vid_" + urlId(u), content_type: "video", url: u, entity_name: entity });
  });
  return sink;
}

// Best-effort post-text capture anchored on permalinks (the most stable DOM
// signal). For each post link we climb to its container and take the text as the
// caption. Engagement counts are left null here — reliable counts need the auth
// API (documented), and we won't fabricate them from brittle DOM scraping.
function harvestPermalinkPosts(linkRe, idFrom) {
  const byId = new Map();
  document.querySelectorAll("a[href]").forEach((a) => {
    const m = (a.getAttribute("href") || "").match(linkRe);
    if (!m) return;
    const pid = idFrom(m);
    if (!pid || byId.has(pid)) return;
    const box = a.closest('[data-pressable-container],[role="article"],article') || a.parentElement;
    let caption = "";
    try { caption = (box && box.innerText || "").trim().slice(0, 2200); } catch (e) {}
    if (!caption) return;                       // skip empties / UI chrome
    byId.set(pid, {
      platform_post_id: pid, caption, media_type: "post",
      hashtags: (caption.match(/#[\w.]+/g) || []).map((s) => s.slice(1)),
      mentions: (caption.match(/@[\w.]+/g) || []).map((s) => s.slice(1)),
    });
  });
  return [...byId.values()];
}

// Collect usernames from profile/permalink anchors (X /<user>/status, FB /<user>/posts,
// Threads /@user) -> social_users. Lightweight, DOM-only, every platform.
function collectPermalinkAuthors(re, skip) {
  const set = new Set();
  document.querySelectorAll("a[href]").forEach((a) => {
    const m = (a.getAttribute("href") || "").match(re);
    if (m && m[1] && m[1].length < 40 && !(skip && skip.test(m[1]))) set.add(m[1]);
  });
  return [...set].map((u) => ({ username: u }));
}

// Switch the Threads home feed to a named tab ("Following" primary, "For you"
// secondary). The switcher is a dropdown behind the top button, so: open it (the
// button shows the *other* feed's name), then click the wanted item. Idempotent.
const THREADS_IMG = { imgRe: /(cdninstagram|fbcdn)\.net/, junkRe: /s150x150|s320x320|profile_pic|rsrc\.php/ };
async function threadsSelectFeed(want) {
  try {
    const other = want === "Following" ? "For you" : "Following";
    const find = (txt) => [...document.querySelectorAll('div[role="button"],a[role="link"],span,div')]
      .find((e) => (e.textContent || "").trim() === txt && e.offsetParent !== null);
    let item = find(want);
    if (!item) { const opener = find(other); if (opener) { opener.click(); await sleep(jitter(1200)); item = find(want); } }
    if (item) { item.click(); await sleep(jitter(2000)); return true; }
  } catch (e) {}
  return false;
}

// NOTE: a Threads handle == the same Meta account's Instagram handle, but NOT every
// Instagram user has activated Threads. So some IG handles 404 on Threads. We detect
// that on the profile page and blacklist the handle (uc_th_noacct) so we never waste
// another navigation on it.
const thNoAcct = () => new Set(lsGet("uc_th_noacct", "").split(",").filter(Boolean));
function thMarkNoAcct(user) {
  const s = thNoAcct(); s.add(user);
  let arr = [...s]; if (arr.length > 1000) arr = arr.slice(-1000);
  lsSet("uc_th_noacct", arr.join(","));
}
// Threads renders a "page isn't available / not found" view for non-existent users.
function threadsProfileMissing() {
  try {
    const t = (document.body.innerText || "").toLowerCase();
    if (/isn['’]t available|page not found|sorry, this page|couldn['’]t find this account/.test(t)) return true;
  } catch (e) {}
  return false;
}

// Reverse cross-pollination picker: rotate through IG-known real handles the bridge
// hands us, skipping recently-visited AND known-no-Threads-account handles.
function pickThreadsNext(pool) {
  if (!pool || !pool.length) return null;
  const dead = thNoAcct();
  let seen = lsGet("uc_th_seen", "").split(",").filter(Boolean);
  let cand = pool.map((t) => t.username).find((u) => u && !seen.includes(u) && !dead.has(u));
  if (!cand) {  // everyone seen -> reset rotation but still skip dead accounts
    seen = [];
    cand = pool.map((t) => t.username).find((u) => u && !dead.has(u));
  }
  if (!cand) return null;
  seen.push(cand);
  if (seen.length > 300) seen = seen.slice(-300);
  lsSet("uc_th_seen", seen.join(","));
  return cand;
}

// Threads (threads.com) — Meta SPA; media served from the Instagram/FB CDN.
const threads = {
  id: "threads", host: "www.threads.com", label: "Threads",
  entity() { const m = location.pathname.match(/^\/@([^/?#]+)/); return m ? m[1] : "feed"; },

  // Scrape one Threads profile we navigated to (the IG→Threads reverse direction).
  async _scrapeProfile(user) {
    clog("info", `Threads profile @${user} — scraping (IG-known real account)`, "threads");
    await autoScroll(8);
    const sink = harvestDom(user, THREADS_IMG);
    if (sink.items.length) await send({ type: "ingest", platform: "threads", username: user, items: sink.items });
    const posts = harvestPermalinkPosts(/\/@([^/]+)\/post\/([^/?#]+)/, (m) => m[2]);
    if (posts.length) {
      await send({ type: "posts", platform: "threads", username: user, posts });
      clog("info", `Threads @${user}: ${posts.length} post(s)`, "threads");
    }
    return { saved: sink.items.length, posts: posts.length };
  },

  async runCycle() {
    // REVERSE direction: if we navigated to a target profile, scrape it then return
    // to the feed so the rotation continues.
    if (/^\/@/.test(location.pathname)) {
      const user = this.entity();
      // this IG handle may not have a Threads account — detect + blacklist so the
      // rotation never wastes another navigation on it.
      if (threadsProfileMissing()) {
        thMarkNoAcct(user);
        clog("info", `Threads @${user}: no Threads account — blacklisted from reverse rotation`, "threads");
      } else {
        const r = await this._scrapeProfile(user);
        // always bounce back to the feed so the rotation keeps moving (robust even if
        // target tracking desyncs); this is a dedicated scraper tab, not manual browsing.
        lsSet("uc_th_target", "");
        await sleep(jitter(4000));
        location.href = "https://www.threads.com/";
        return { targets: 1, saved: r.saved, posts: r.posts, discovered: 0 };
      }
      lsSet("uc_th_target", "");
      await sleep(jitter(2500));
      location.href = "https://www.threads.com/";
      return { targets: 1, saved: 0, posts: 0, discovered: 0 };
    }

    // FEED: Following primary; For-You every 4th cycle (secondary).
    const c = lsBump("uc_th_cyc");
    const feed = (c % 4 === 0)
      ? ((await threadsSelectFeed("For you")) ? "foryou" : "feed")
      : ((await threadsSelectFeed("Following")) ? "following" : "feed");
    clog("info", `Threads cycle on ${feed} — scrolling`, "threads");
    await autoScroll(10);
    const sink = harvestDom(feed, THREADS_IMG);
    if (sink.items.length) await send({ type: "ingest", platform: "threads", username: feed, items: sink.items });
    const posts = harvestPermalinkPosts(/\/@([^/]+)\/post\/([^/?#]+)/, (m) => m[2]);
    if (posts.length) {
      await send({ type: "posts", platform: "threads", username: feed, posts });
      clog("info", `Threads ${feed}: ${posts.length} post(s)`, "threads");
    }
    // every threads handle IS an instagram handle — push feed authors into the
    // shared user graph so IG scrapes them too (forward cross-pollination).
    const authors = collectPermalinkAuthors(/^\/@([A-Za-z0-9._]{1,30})(?:\/|$)/, /^(search|explore|activity|saved)$/);
    if (authors.length) await send({ type: "users", platform: "threads", context: feed, users: authors });

    // REVERSE direction rotation: every 3rd cycle, hop to one IG-known real account
    // and scrape their Threads. Heavily paced (one profile per rotation) to stay gentle.
    if (c % 3 === 0) {
      try {
        const resp = (await send({ type: "getTargets", platform: "threads" })) || [];
        const pool = (Array.isArray(resp) ? resp : (resp.targets || []))
          .map((t) => (typeof t === "string" ? { username: t } : t)).filter((t) => t && t.username);
        const next = pickThreadsNext(pool);
        if (next) {
          lsSet("uc_th_target", next);
          clog("info", `Threads → visiting IG-known @${next} (reverse cross-pollination)`, "threads");
          await sleep(jitter(3000));
          location.href = "https://www.threads.com/@" + next;
        }
      } catch (e) {}
    }
    return { targets: 1, saved: sink.items.length, posts: posts.length, discovered: authors.length };
  },
};

// Facebook — DOM media from fbcdn; noisy (lots of UI chrome), so the size gate
// does the heavy lifting. Open your feed / a profile's Photos tab and scroll.
const facebook = {
  id: "facebook", host: "www.facebook.com", label: "Facebook",
  entity() { const m = location.pathname.match(/^\/([^/?#]+)/); return m && !/^(home|watch|marketplace|groups|friends|notifications)$/.test(m[1]) ? m[1] : "feed"; },
  // Download MEDIA only from a REAL PERSON's profile (user: not pages, not groups).
  // Heuristic: a profile URL showing friend UI ("Add friend"/"Friends"/"Mutual"),
  // without page UI ("follow this Page"/"Send message" to a Page). Conservative —
  // when unsure we DON'T download (avoids page/group media).
  _isPerson() {
    const path = location.pathname;
    if (/\/(groups|watch|marketplace|reels|events|gaming|pages)\b/.test(path) || /^\/(home)?$/.test(path)) return false;
    const isProfilePath = /\/profile\.php/.test(path) || /^\/[A-Za-z0-9.]+\/?$/.test(path);
    if (!isProfilePath) return false;
    const txt = (document.body.innerText || "").slice(0, 20000);
    const personSig = /\bAdd friend\b|\bFriends\b|\bMutual friends?\b/i.test(txt);
    const pageSig = /people follow this|Like this Page|\bThis Page\b|·\s*Follower/i.test(txt);
    return personSig && !pageSig;
  },
  async runCycle() {
    const entity = this.entity();
    const person = this._isPerson();
    clog("info", `cycle start on ${entity} (person profile: ${person})`, "facebook");
    await autoScroll(12);
    let saved = 0;
    // MEDIA — only real people's profiles.
    if (person) {
      const sink = harvestDom(entity, { imgRe: /fbcdn\.net/, junkRe: /rsrc\.php|emoji|static|\/s\d+x\d+\/|profile|sprite/ });
      if (sink.items.length) { await send({ type: "ingest", platform: "facebook", username: entity, items: sink.items }); saved = sink.items.length; }
    }
    // POSTS (captions) + USERS — captured EVERYWHERE incl. pages/groups, for the
    // user registry + spidering (user: "when spider we can use either").
    const posts = harvestPermalinkPosts(/\/(?:posts\/|permalink\.php\?story_fbid=|[^/]+\/posts\/)?(pfbid[\w]+|\d{6,})/, (m) => m[1]);
    if (posts.length) await send({ type: "posts", platform: "facebook", username: entity, posts });
    const fu = collectPermalinkAuthors(
      /^\/([A-Za-z0-9.]{5,40})(?:\/(posts|photos|videos))?(?:[/?]|$)/,
      /^(home|watch|marketplace|groups|friends|notifications|messages|reels|events|gaming|bookmarks|stories|pages|story\.php|permalink\.php|profile\.php|sharer|login|policies)$/
    );
    if (fu.length) await send({ type: "users", platform: "facebook", context: "seen", users: fu });
    return { targets: 1, saved, discovered: 0 };
  },
};

// ===========================================================================
// Registry + dispatch
// ===========================================================================
const PLATFORMS = [instagram, tiktok, lemon8, x, threads, facebook];

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
          setWall(p.id, mins);  // persist so a respawned loop won't immediately re-hit it
          clog("warn", `${p.label} hit a throttle/login wall — sleeping ${mins}m (persisted, survives refresh)`, p.label);
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

// Relay engagement posts captured by the MAIN-world network hook (inject.js).
// inject.js reads Meta's own GraphQL/REST responses (likes/replies/reposts) and
// postMessages them here; we forward to the posts endpoint. Robust, no extra reqs.
window.addEventListener("message", (ev) => {
  const m = ev.data;
  if (!m || m.__uc !== true) return;
  // label the source so logs say WHERE it came from instead of "undefined".
  const where = (() => { try { return (location.pathname.match(/^\/@?([^/?#]+)/) || [, "feed"])[1].slice(0, 30); } catch (e) { return "feed"; } })();
  if (m.type === "posts" && Array.isArray(m.posts) && m.posts.length) {
    send({ type: "posts", platform: m.platform, username: where, posts: m.posts }).catch(() => {});
  } else if (m.type === "users" && Array.isArray(m.users) && m.users.length) {
    send({ type: "users", platform: m.platform, context: m.context || "seen", users: m.users }).catch(() => {});
  }
});

// Auto-start the loop the moment the tab loads (respawns after a reload/crash).
send({ type: "tabReady", platform: (currentPlatform() || {}).id }).catch(() => {});
mainLoop();
