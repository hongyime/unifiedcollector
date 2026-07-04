// MAIN-world network hook. Runs in the PAGE's JS context (not the isolated
// content-script world) so it can wrap window.fetch + XMLHttpRequest and read the
// JSON the page already fetches from Meta's own APIs. This is the robust, ban-safe
// way to get engagement counts (likes/replies/reposts) for Threads & Instagram —
// we read Meta's real responses instead of replicating signed GraphQL calls.
//
// It does NOT issue any extra requests; it only observes responses the page makes.
// Harvested post records are handed to the content script via window.postMessage.
(function () {
  if (window.__UC_HOOKED__) return;
  window.__UC_HOOKED__ = true;

  const HOST = location.hostname;
  const platform = /threads\.com$/.test(HOST) ? "threads"
    : /instagram\.com$/.test(HOST) ? "instagram"
    : /(^|\.)x\.com$/.test(HOST) || /twitter\.com$/.test(HOST) ? "x"
    : /facebook\.com$/.test(HOST) ? "facebook"
    : /tiktok\.com$/.test(HOST) ? "tiktok" : null;
  if (!platform) return;

  function emit(posts) {
    if (posts && posts.length) {
      window.postMessage({ __uc: true, type: "posts", platform, posts }, "*");
    }
  }
  function emitUsers(users, context) {
    if (users && users.length) {
      window.postMessage({ __uc: true, type: "users", platform, context: context || "seen", users }, "*");
    }
  }

  // TikTok signs its API requests, so we OBSERVE the follower/following modal's
  // /api/user/list/ response (no extra requests) rather than replicate the signed
  // call. scene tells us which side: 21 = following (I follow), 67 = followers
  // (follows me). Users get the 'follow'/'follower' relationship context so the
  // owner's own list becomes their real graph in social_users.
  const _emittedTk = new Set();
  function harvestTikTokList(url, text) {
    if (!text || text.length > 6_000_000) return;
    let json; try { json = JSON.parse(text); } catch (e) { return; }
    const list = json && (json.userList || json.user_list);
    if (!Array.isArray(list) || !list.length) return;
    let scene = null;
    try { scene = new URL(url, location.href).searchParams.get("scene"); } catch (e) {}
    const context = scene === "67" ? "follower" : scene === "21" ? "follow" : "seen";
    const users = [];
    for (const item of list) {
      const u = (item && (item.user || item)) || {};
      const uname = u.uniqueId || u.unique_id;
      if (!uname) continue;
      const id = u.id || u.uid || u.secUid;
      const k = context + ":" + (id || uname);
      if (_emittedTk.has(k)) continue;
      if (_emittedTk.size > 8000) _emittedTk.clear();
      _emittedTk.add(k);
      users.push({ user_id: id != null ? String(id) : null, username: uname,
                   display_name: u.nickname || null, profile_pic_url: u.avatarThumb || u.avatarMedium || null });
    }
    if (users.length) emitUsers(users, context);
  }
  // any object that looks like a user (username + id) is someone we've "seen"
  // — commenters, likers, reactors, taggers, followers the page loaded.
  function userFrom(o) {
    if (!o || typeof o !== "object") return null;
    const username = o.username || o.screen_name;       // X uses screen_name
    const pk = o.pk || o.id || o.rest_id;
    if (!username || typeof username !== "string") return null;
    return { user_id: pk != null ? String(pk) : null, username,
             display_name: o.full_name || o.name || null,
             profile_pic_url: o.profile_pic_url || o.profile_image_url_https || null };
  }

  // Pull engagement off a Threads/IG post node (handles both shapes defensively).
  function postFrom(node) {
    if (!node || typeof node !== "object") return null;
    const pk = node.pk || node.id || node.code;
    const caption = node.caption && (node.caption.text || (typeof node.caption === "string" ? node.caption : null));
    const tpa = node.text_post_app_info || {};
    const likes = node.like_count;
    const replies = tpa.direct_reply_count ?? node.comment_count;
    const reposts = tpa.repost_count ?? tpa.reshare_count ?? node.reshare_count;
    // require a real post signal so we don't emit noise
    if (!pk || (likes === undefined && caption == null && replies === undefined)) return null;
    const user = node.user || {};
    return {
      platform_post_id: String(pk),
      author_username: user.username || null,
      caption: caption || null,
      likes_count: typeof likes === "number" ? likes : null,
      comments_count: typeof replies === "number" ? replies : null,
      reposts_count: typeof reposts === "number" ? reposts : null,
      taken_at: node.taken_at || node.taken_at_timestamp || null,
    };
  }

  // X/Twitter tweet node (GraphQL): engagement lives in obj.legacy.*
  function tweetFrom(obj) {
    const lg = obj.legacy;
    if (!lg || lg.favorite_count === undefined) return null;
    const pid = obj.rest_id || lg.id_str;
    if (!pid) return null;
    let author = null;
    try { author = obj.core.user_results.result.legacy.screen_name; } catch (e) {}
    return {
      platform_post_id: String(pid),
      author_username: author,
      caption: lg.full_text || null,
      likes_count: lg.favorite_count ?? null,
      comments_count: lg.reply_count ?? null,
      reposts_count: lg.retweet_count ?? null,
      quote_count: lg.quote_count ?? null,
      views_count: (obj.views && (obj.views.count ? parseInt(obj.views.count, 10) : null)) ?? null,
      taken_at: lg.created_at ? Math.floor(Date.parse(lg.created_at) / 1000) : null,
    };
  }

  function scan(obj, out, users, depth) {
    if (!obj || depth > 9 || (out.length > 400 && users.length > 1500)) return;
    if (Array.isArray(obj)) { for (const v of obj) scan(v, out, users, depth + 1); return; }
    if (typeof obj !== "object") return;
    if (platform === "x" && obj.legacy && obj.legacy.favorite_count !== undefined) {
      const t = tweetFrom(obj);
      if (t) out.push(t);
    } else if (platform === "facebook" && obj.reaction_count && typeof obj.reaction_count.count === "number") {
      // FB feedback node carries engagement; id from the story it belongs to.
      const pid = obj.subscription_target_id || obj.associated_group_id || obj.share_fbid || obj.id;
      if (pid) {
        const comments = (obj.total_comment_count != null ? obj.total_comment_count
          : (obj.comment_rendering_instance && obj.comment_rendering_instance.comments && obj.comment_rendering_instance.comments.total_count));
        out.push({
          platform_post_id: String(pid),
          likes_count: obj.reaction_count.count,
          comments_count: typeof comments === "number" ? comments : null,
          shares_count: (obj.share_count && obj.share_count.count) ?? (obj.reshare_count && obj.reshare_count.count) ?? null,
        });
      }
    } else if (obj.like_count !== undefined || obj.text_post_app_info || (obj.caption && obj.pk)) {
      const p = postFrom(obj);
      if (p) out.push(p);
    }
    if ((obj.username || obj.screen_name) && (obj.pk !== undefined || obj.id !== undefined || obj.rest_id !== undefined)) {
      const u = userFrom(obj);
      if (u) users.push(u);
    }
    for (const k in obj) { const v = obj[k]; if (v && typeof v === "object") scan(v, out, users, depth + 1); }
  }

  // Session-level dedup so the SAME post/user isn't re-emitted on every GraphQL
  // response (the feed re-fetches constantly -> was spamming "posts[threads] saved 4").
  const _emittedP = new Set(), _emittedU = new Set();
  const _cap = (s) => { if (s.size > 5000) s.clear(); };

  function harvestText(text) {
    if (!text || text.length > 6_000_000) return;
    let json;
    try { json = JSON.parse(text); } catch (e) { return; }
    const out = [], users = [];
    scan(json, out, users, 0);
    _cap(_emittedP); _cap(_emittedU);
    emit(out.filter((p) => (_emittedP.has(p.platform_post_id) ? false : _emittedP.add(p.platform_post_id))));
    emitUsers(users.filter((u) => { const k = u.user_id || u.username; return _emittedU.has(k) ? false : _emittedU.add(k); }));
  }

  const apiRe = /graphql|\/api\/v1\//;  // IG/Threads /api/graphql + X /i/api/graphql

  const tkListRe = /\/api\/user\/list\//;  // TikTok follower/following modal

  const origFetch = window.fetch;
  window.fetch = function (...args) {
    const p = origFetch.apply(this, args);
    try {
      const url = String((args[0] && (args[0].url || args[0])) || "");
      if (platform === "tiktok" && tkListRe.test(url)) {
        p.then((r) => { try { r.clone().text().then((t) => harvestTikTokList(url, t)).catch(() => {}); } catch (e) {} }).catch(() => {});
      } else if (apiRe.test(url)) {
        p.then((r) => { try { r.clone().text().then(harvestText).catch(() => {}); } catch (e) {} }).catch(() => {});
      }
    } catch (e) {}
    return p;
  };

  const OrigOpen = XMLHttpRequest.prototype.open;
  const OrigSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__uc_url = url;
    return OrigOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    try {
      const url = String(this.__uc_url || "");
      if (platform === "tiktok" && tkListRe.test(url)) {
        this.addEventListener("load", function () {
          try { if (typeof this.responseText === "string") harvestTikTokList(url, this.responseText); } catch (e) {}
        });
      } else if (apiRe.test(url)) {
        this.addEventListener("load", function () {
          try { if (typeof this.responseText === "string") harvestText(this.responseText); } catch (e) {}
        });
      }
    } catch (e) {}
    return OrigSend.apply(this, arguments);
  };
})();
