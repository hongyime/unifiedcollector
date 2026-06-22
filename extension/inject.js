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
  const platform = /threads\.com$/.test(HOST) ? "threads" : /instagram\.com$/.test(HOST) ? "instagram" : null;
  if (!platform) return;

  function emit(posts) {
    if (posts && posts.length) {
      window.postMessage({ __uc: true, type: "posts", platform, posts }, "*");
    }
  }
  function emitUsers(users) {
    if (users && users.length) {
      window.postMessage({ __uc: true, type: "users", platform, context: "seen", users }, "*");
    }
  }
  // any object that looks like a user (username + id) is someone we've "seen"
  // — commenters, likers, reactors, taggers, followers the page loaded.
  function userFrom(o) {
    if (!o || typeof o !== "object") return null;
    const username = o.username;
    const pk = o.pk || o.id;
    if (!username || typeof username !== "string") return null;
    return { user_id: pk != null ? String(pk) : null, username, display_name: o.full_name || null };
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

  function scan(obj, out, users, depth) {
    if (!obj || depth > 9 || (out.length > 400 && users.length > 1500)) return;
    if (Array.isArray(obj)) { for (const v of obj) scan(v, out, users, depth + 1); return; }
    if (typeof obj !== "object") return;
    if (obj.like_count !== undefined || obj.text_post_app_info || (obj.caption && obj.pk)) {
      const p = postFrom(obj);
      if (p) out.push(p);
    }
    if (obj.username && (obj.pk !== undefined || obj.id !== undefined)) {
      const u = userFrom(obj);
      if (u) users.push(u);
    }
    for (const k in obj) { const v = obj[k]; if (v && typeof v === "object") scan(v, out, users, depth + 1); }
  }

  function harvestText(text) {
    if (!text || text.length > 6_000_000) return;
    let json;
    try { json = JSON.parse(text); } catch (e) { return; }
    const out = [], users = [];
    scan(json, out, users, 0);
    const seenP = new Set();
    emit(out.filter((p) => (seenP.has(p.platform_post_id) ? false : seenP.add(p.platform_post_id))));
    const seenU = new Set();
    emitUsers(users.filter((u) => { const k = u.user_id || u.username; return seenU.has(k) ? false : seenU.add(k); }));
  }

  const apiRe = /\/api\/(graphql|v1\/)/;

  const origFetch = window.fetch;
  window.fetch = function (...args) {
    const p = origFetch.apply(this, args);
    try {
      const url = (args[0] && (args[0].url || args[0])) || "";
      if (apiRe.test(String(url))) {
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
      if (apiRe.test(String(this.__uc_url || ""))) {
        this.addEventListener("load", function () {
          try { if (typeof this.responseText === "string") harvestText(this.responseText); } catch (e) {}
        });
      }
    } catch (e) {}
    return OrigSend.apply(this, arguments);
  };
})();
