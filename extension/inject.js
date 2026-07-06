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
  function emitUsers(users, context, owner) {
    if (users && users.length) {
      window.postMessage({ __uc: true, type: "users", platform, context: context || "seen", owner: owner || null, users }, "*");
    }
  }

  // TikTok signs its API requests, so we OBSERVE the follower/following modal's
  // /api/user/list/ response (no extra requests) rather than replicate the signed
  // call. scene tells us which side: 21 = following (I follow), 67 = followers
  // (follows me). Users get the 'follow'/'follower' relationship context so the
  // owner's own list becomes their real graph in social_users.
  // Resolve the logged-in TikTok owner, but ONLY when the profile being VIEWED is
  // that same account (its own follower/following list) — so we never mis-attribute
  // someone else's followers to your per-account graph (follow_edges).
  function _tiktokOwner() {
    try {
      let me = null;
      const uni = window.__UNIVERSAL_DATA_FOR_REHYDRATION__;
      if (uni && uni["__DEFAULT_SCOPE__"]) {
        const scope = uni["__DEFAULT_SCOPE__"];
        me = (scope["webapp.app-context"] && scope["webapp.app-context"].user && scope["webapp.app-context"].user.uniqueId)
          || (scope["webapp.user-detail"] && scope["webapp.user-detail"].userInfo && scope["webapp.user-detail"].userInfo.user && scope["webapp.user-detail"].userInfo.user.uniqueId)
          || null;
      }
      if (!me && window.SIGI_STATE && window.SIGI_STATE.AppContext && window.SIGI_STATE.AppContext.user) {
        me = window.SIGI_STATE.AppContext.user.uniqueId || null;
      }
      if (!me) return null;
      const viewed = (location.pathname.match(/^\/@([^/?#]+)/) || [])[1];
      if (viewed && viewed.toLowerCase() === String(me).toLowerCase()) return { username: me };
      return null;
    } catch (e) { return null; }
  }

  const _emittedTk = new Set();
  function harvestTikTokList(url, text) {
    if (!text || text.length > 6_000_000) return;
    let json; try { json = JSON.parse(text); } catch (e) { return; }
    const list = json && (json.userList || json.user_list);
    if (!Array.isArray(list) || !list.length) return;
    let scene = null;
    try { scene = new URL(url, location.href).searchParams.get("scene"); } catch (e) {}
    const context = scene === "67" ? "follower" : scene === "21" ? "follow" : "seen";
    const owner = context !== "seen" ? _tiktokOwner() : null;
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
    if (users.length) emitUsers(users, context, owner);
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
  const dmRe = /\/direct_v2\/(inbox|threads|thread)/;  // IG DMs (observe-only)

  // Investigation probe: IG DMs weren't reaching the bridge via the direct_v2
  // HTTP path, so log EVERY IG url that mentions "direct" (once per distinct
  // path) to learn the real endpoint, and — since IG likely pushes DMs over a
  // WebSocket (edge-chat MQTT) like TikTok's frontier WS — the WS hook below now
  // covers instagram too. All observe-only (no extra requests).
  const _probedUrls = new Set();
  function probeDmUrl(url) {
    try {
      if (!/direct/i.test(url)) return;
      const key = String(url).split("?")[0];
      if (_probedUrls.has(key)) return;
      _probedUrls.add(key);
      window.postMessage({ __uc: true, type: "dm_probe", platform, transport: "http", url: key.slice(0, 300) }, "*");
    } catch (e) {}
  }

  // Parse IG DM inbox/thread responses the page already fetched -> emit messages.
  // Ban-safe: no extra requests. owner = ds_user_id so ig_ingest can set is_from_me.
  function harvestDMs(text) {
    if (!text || text.length > 8_000_000) return;
    let json; try { json = JSON.parse(text); } catch (e) { return; }
    let threads = [];
    if (json.inbox && Array.isArray(json.inbox.threads)) threads = json.inbox.threads;
    else if (json.thread) threads = [json.thread];
    else if (Array.isArray(json.threads)) threads = json.threads;
    if (!threads.length) return;
    let ownerId = null;
    try { ownerId = (document.cookie.match(/ds_user_id=(\d+)/) || [])[1] || null; } catch (e) {}
    window.postMessage({ __uc: true, type: "dms", platform: "instagram", owner: { id: ownerId }, threads }, "*");
  }

  // Modern IG web has been migrating DM inbox/thread fetches off /direct_v2/
  // to /api/graphql/ (and /graphql/query/). harvestDMs above only fires on
  // direct_v2 URLs. This function heuristically extracts DM data from ANY
  // instagram response body that looks like it contains it — checks for a
  // couple of guaranteed DM structural markers before shipping. Emits a
  // dm_probe on match so we log which URLs are actually carrying the data,
  // then a dms message for the harvester downstream.
  //
  // Investigation phase — will graduate to a real per-URL harvester once
  // we see which query names IG uses. Bounded by response-body size and
  // marker checks to avoid processing every GraphQL response on the page.
  const igGraphQLRe = /\/(?:api\/graphql|graphql\/query|ajax\/route-definition|ajax\/bulk-route-definitions)/;
  function harvestIGGraphQL(url, text) {
    try {
      if (!text || text.length > 8_000_000) return;
      // Cheap marker check — GraphQL responses that DON'T mention any DM
      // structure are the vast majority; skip them without JSON-parsing.
      if (!/"thread_id"|"direct_message"|"conversation_id"|"inbox"/.test(text)) return;
      let json; try { json = JSON.parse(text); } catch (e) { return; }
      // Log the URL as a probe so we can inventory which GraphQL query
      // names IG uses for DMs. Cheap — no DB write yet.
      window.postMessage({ __uc: true, type: "dm_probe", platform: "instagram",
        transport: "graphql", url: url.slice(0, 300), frame_kind: "json",
        frame_size: text.length }, "*");
      // Best-effort structural extraction — the exact GraphQL response shape
      // varies by query and version. Handle a few known containers before
      // giving up; leave the full text as a follow-up sample if none match.
      let threads = null;
      const roots = [json, json && json.data, json && json.viewer,
                     json && json.data && json.data.viewer];
      for (const root of roots) {
        if (!root) continue;
        if (root.inbox && Array.isArray(root.inbox.threads))
          threads = root.inbox.threads;
        else if (root.direct_inbox && Array.isArray(root.direct_inbox.threads))
          threads = root.direct_inbox.threads;
        else if (root.thread) threads = [root.thread];
        else if (Array.isArray(root.threads)) threads = root.threads;
        if (threads && threads.length) break;
      }
      if (!threads || !threads.length) return;
      let ownerId = null;
      try { ownerId = (document.cookie.match(/ds_user_id=(\d+)/) || [])[1] || null; }
      catch (e) {}
      window.postMessage({ __uc: true, type: "dms", platform: "instagram",
        owner: { id: ownerId }, threads }, "*");
    } catch (e) {}
  }

  const origFetch = window.fetch;
  window.fetch = function (...args) {
    const p = origFetch.apply(this, args);
    try {
      const url = String((args[0] && (args[0].url || args[0])) || "");
      if (platform === "tiktok" && tkListRe.test(url)) {
        p.then((r) => { try { r.clone().text().then((t) => harvestTikTokList(url, t)).catch(() => {}); } catch (e) {} }).catch(() => {});
      } else if (platform === "instagram" && dmRe.test(url)) {
        p.then((r) => { try { r.clone().text().then(harvestDMs).catch(() => {}); } catch (e) {} }).catch(() => {});
      } else if (platform === "instagram" && /direct/i.test(url)) {
        probeDmUrl(url);  // investigation: a direct URL that dmRe didn't match
        p.then((r) => { try { r.clone().text().then(harvestDMs).catch(() => {}); } catch (e) {} }).catch(() => {});
      } else if (platform === "instagram" && igGraphQLRe.test(url)) {
        // Modern IG web DM path — investigate whether these responses
        // contain the inbox/thread data direct_v2 used to serve.
        p.then((r) => { try { r.clone().text().then((t) => harvestIGGraphQL(url, t)).catch(() => {}); } catch (e) {} }).catch(() => {});
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
      } else if (platform === "instagram" && dmRe.test(url)) {
        this.addEventListener("load", function () {
          try { if (typeof this.responseText === "string") harvestDMs(this.responseText); } catch (e) {}
        });
      } else if (platform === "instagram" && /direct/i.test(url)) {
        probeDmUrl(url);  // investigation: a direct URL that dmRe didn't match
        this.addEventListener("load", function () {
          try { if (typeof this.responseText === "string") harvestDMs(this.responseText); } catch (e) {}
        });
      } else if (platform === "instagram" && igGraphQLRe.test(url)) {
        // XHR-path mirror of the fetch-path GraphQL harvester above.
        this.addEventListener("load", function () {
          try { if (typeof this.responseText === "string") harvestIGGraphQL(url, this.responseText); } catch (e) {}
        });
      } else if (apiRe.test(url)) {
        this.addEventListener("load", function () {
          try { if (typeof this.responseText === "string") harvestText(this.responseText); } catch (e) {}
        });
      }
    } catch (e) {}
    return OrigSend.apply(this, arguments);
  };

  // ── DM investigation (#38): observe-only WebSocket hook ──────────────────
  // Neither TikTok nor Instagram deliver realtime DMs over the JSON HTTP paths
  // the fetch/XHR hooks watch. TikTok uses a "frontier" WS (wss://im-ws-…/ws/…,
  // binary protobuf); Instagram uses an edge-chat MQTT WS (wss://edge-chat.
  // instagram.com/chat, binary). This wraps WebSocket (passive — we send
  // nothing, so it stays ban-safe) and, per distinct socket, (a) captures any
  // JSON frames as `<platform>_dm`, and (b) emits a one-time `dm_probe`
  // describing binary frames so we can confirm the wire format and add a
  // decoder later (#35). We probe ALL sockets on these platforms so we don't
  // miss the DM socket by guessing its URL.
  if (platform === "tiktok" || platform === "instagram") {
    try {
      const OrigWS = window.WebSocket;
      const _wsProbed = new Set();
      // The two real DM sockets (edge-chat MQTT for IG, im-ws frontier for
      // TikTok). We only raw-sample frames on THESE (not the chatty keepalive
      // gateway sockets), above a size threshold (skip 1-4B pings), capped per
      // socket, so I can confirm the payload format and build a decoder (#35).
      const _dmSockRe = /edge-chat\.instagram\.com\/chat|im-ws[^/]*\.tiktok\.com\/ws/i;
      const _sampleCount = new Map();
      // SAMPLE_MAX was originally 6 (~"confirm the wire format is protobuf"
      // fits in 6 handshake frames). But TikTok's frontier socket opens with
      // ~6 stereotyped session-status frames before any user traffic arrives,
      // so the cap always hit on session-init and every real DM after that
      // was dropped — verified empirically: 61 captured samples were byte-
      // identical PayloadRelatedMethod=20032 pushes with only 27 distinct
      // inner sha256 values across hours of capture, zero message content.
      // Now that the bridge's P1.1 rotation keeps only the newest 200 files
      // per platform, we can afford a much looser per-session cap; the
      // 24-byte SAMPLE_MIN_BYTES floor still skips MQTT/heartbeat pings.
      const SAMPLE_MAX = 200, SAMPLE_MIN_BYTES = 24;
      // IG-specific min bytes: IG's edge-chat MQTT sends CONNACK/PING at
      // 2-4B and legitimate small PUBLISH control frames at 5-15B. Setting
      // the floor to 8 catches those without pulling in every PING; sample
      // rotation still bounds disk. Zero IG samples have crossed 24B in
      // ~10 sockets of observed traffic so far — either IG's realtime DM
      // content flows via GraphQL (see harvestIGGraphQL below) instead of
      // MQTT, or the real DM PUBLISH frames are on a topic we haven't
      // observed yet. Lower threshold lets us LEARN which is true.
      const IG_SAMPLE_MIN_BYTES = 8;
      // P1.3: WS-hook heartbeat counters. shipSample/probe increment these;
      // a setInterval below emits a heartbeat postMessage so background.js
      // can POST /social/dm-heartbeat and the watchdog can detect a broken
      // hook (IG/TikTok bundle updates silently disable the wrapper today).
      let _hookProbes = 0;
      let _hookSamples = 0;
      function abToB64(buf) {
        let s = ""; const b = new Uint8Array(buf);
        for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
        return btoa(s);
      }
      function shipSample(u, key, buf) {
        try {
          const n = _sampleCount.get(key) || 0;
          if (n >= SAMPLE_MAX || !buf) return;
          const minBytes = platform === "instagram" ? IG_SAMPLE_MIN_BYTES : SAMPLE_MIN_BYTES;
          if (buf.byteLength < minBytes) return;
          _sampleCount.set(key, n + 1);
          _hookSamples++;
          window.postMessage({ __uc: true, type: "dm_sample", platform,
            url: u.slice(0, 200), size: buf.byteLength, b64: abToB64(buf) }, "*");
        } catch (e) {}
      }

      // ── Minimal in-tab protobuf reader (Option B of #39, TikTok decoder) ──
      // Enough to walk the TikTok frontier envelope + inner message wrapper.
      // Wire format primer:
      //   tag varint = (field_number << 3) | wire_type
      //   wire_type: 0=varint, 1=64-bit fixed, 2=length-delimited, 5=32-bit
      // We only need varint + length-delimited for the TikTok frames we've
      // reverse-engineered; other wires are decoded to raw bytes so they
      // don't crash the walker on schema drift.
      function _pbReadVarint(buf, i) {
        let result = 0n, shift = 0n;
        while (i < buf.length) {
          const b = buf[i++];
          result |= BigInt(b & 0x7f) << shift;
          if (!(b & 0x80)) return [result, i];
          shift += 7n;
          if (shift > 63n) throw new Error("varint too long");
        }
        throw new Error("varint truncated");
      }
      function _pbDecode(buf) {
        // Returns Array<[fieldNum, wire, value]>. Value is BigInt for varint,
        // Uint8Array for length-delimited, number for 32/64-bit fixed. Non-
        // fatal on truncation — returns what it parsed cleanly.
        const out = [];
        let i = 0;
        while (i < buf.length) {
          let tag, next;
          try {
            [tag, next] = _pbReadVarint(buf, i);
          } catch (e) { break; }
          i = next;
          const fn = Number(tag >> 3n);
          const wire = Number(tag & 0x7n);
          if (fn === 0) break;
          if (wire === 0) {
            let v; try { [v, i] = _pbReadVarint(buf, i); } catch (e) { break; }
            out.push([fn, 0, v]);
          } else if (wire === 2) {
            let len; try { [len, i] = _pbReadVarint(buf, i); } catch (e) { break; }
            const L = Number(len);
            if (i + L > buf.length) break;
            out.push([fn, 2, buf.subarray(i, i + L)]);
            i += L;
          } else if (wire === 1) {
            if (i + 8 > buf.length) break;
            i += 8; out.push([fn, 1, null]);
          } else if (wire === 5) {
            if (i + 4 > buf.length) break;
            i += 4; out.push([fn, 5, null]);
          } else {
            break;
          }
        }
        return out;
      }
      function _pbFindFirst(fields, fnum, wire) {
        for (const f of fields) if (f[0] === fnum && f[1] === wire) return f[2];
        return undefined;
      }
      function _pbFindAll(fields, fnum, wire) {
        const out = [];
        for (const f of fields) if (f[0] === fnum && f[1] === wire) out.push(f[2]);
        return out;
      }
      function _bufToUtf8(u8) {
        try { return new TextDecoder("utf-8", { fatal: false }).decode(u8); }
        catch (e) { return null; }
      }
      // Big-uint → string preserves TikTok's uint64 IDs (message_id, sender_uid)
      // that can't fit in a JS Number without precision loss.
      function _bigToStr(b) { return typeof b === "bigint" ? b.toString() : String(b); }

      // TikTok frontier DM decoder. Given a raw binary WS frame, returns
      // {threads: [...], messages: [...]} shaped for POST /social/dm-decoded,
      // or null when the frame isn't a user-event message. Structure derived
      // empirically — see tmp/dm_analysis/decode_raw.py + inspect_unknown.py
      // and src/db/migrations/add_tiktok_dm.sql for the field map.
      function _ttDecode(buf) {
        try {
          const u8 = new Uint8Array(buf);
          const outer = _pbDecode(u8);
          // Only decode method=5 (user event); method=20032 is server heartbeat.
          const method = _pbFindFirst(outer, 3, 0);
          if (method === undefined || Number(method) !== 5) return null;
          const inner = _pbFindFirst(outer, 8, 2);
          if (!inner) return null;
          const innerFields = _pbDecode(inner);
          // field 6 → wrapper containing field 500 → message envelope(s).
          const wrap = _pbFindFirst(innerFields, 6, 2);
          if (!wrap) return null;
          const wrapFields = _pbDecode(wrap);
          const envelopes = _pbFindAll(wrapFields, 500, 2);
          if (!envelopes.length) return null;
          const threads = [];
          const messages = [];
          const seenThreads = new Set();
          for (const env of envelopes) {
            const envF = _pbDecode(env);
            const cid = _pbFindFirst(envF, 2, 2);
            const conversationId = cid ? _bufToUtf8(cid) : null;
            const convType = _pbFindFirst(envF, 3, 0);
            // repeated field 5 = message entries
            for (const entry of _pbFindAll(envF, 5, 2)) {
              const eF = _pbDecode(entry);
              // The inner conversation_id (field 1) matches the outer one on
              // real messages; still prefer it as the source of truth.
              const inCid = _pbFindFirst(eF, 1, 2);
              const entryCid = inCid ? _bufToUtf8(inCid) : conversationId;
              const mid = _pbFindFirst(eF, 3, 0);
              const msgType = _pbFindFirst(eF, 6, 0);
              const senderUid = _pbFindFirst(eF, 7, 0);
              const contentBuf = _pbFindFirst(eF, 8, 2);
              const createTimeMs = _pbFindFirst(eF, 10, 0);
              const secUidBuf = _pbFindFirst(eF, 14, 2);
              const contentStr = contentBuf ? _bufToUtf8(contentBuf) : null;
              let contentJson = null;
              try { contentJson = contentStr ? JSON.parse(contentStr) : null; }
              catch (e) { contentJson = null; }
              // Skip RPC-style events (command_type is set) — mark_read, typing
              // indicators, etc. — those aren't user messages and would pollute
              // the tiktok_dm table with 'text=null' rows keyed on RPC IDs.
              if (contentJson && typeof contentJson === "object"
                  && "command_type" in contentJson) continue;
              // Speculative media URL extraction for aweType != 0. Field names
              // are best-effort from public reverse engineering; unknown
              // shapes fall through to media_url=null (raw_content is
              // preserved so post-hoc extraction can still recover the URL).
              // Handled aweTypes: 1=sticker, 2=image, 3=video, 5=audio,
              // 6=gif, 7=share.
              let mediaUrl = null;
              if (contentJson && typeof contentJson === "object") {
                const j = contentJson;
                const aw = typeof j.aweType === "number" ? j.aweType : -1;
                if (aw === 1) mediaUrl = j.stickerUrl
                  || (j.imageInfo && (j.imageInfo.url || j.imageInfo.imageUri)) || null;
                else if (aw === 2) mediaUrl = j.imageUri || j.display_image
                  || (j.imageInfo && (j.imageInfo.imageUri || j.imageInfo.url)) || null;
                else if (aw === 3) mediaUrl = (j.videoInfo &&
                  (j.videoInfo.playAddr || j.videoInfo.playUri || j.videoInfo.url))
                  || j.videoUrl || null;
                else if (aw === 5) mediaUrl = (j.audioInfo &&
                  (j.audioInfo.playUrl || j.audioInfo.url)) || j.audioUrl || null;
                else if (aw === 6) mediaUrl = j.giphyUrl || j.gifUrl
                  || (j.gifInfo && (j.gifInfo.url || j.gifInfo.playAddr)) || null;
                else if (aw === 7) mediaUrl = (j.item && j.item.share
                    && j.item.share.share_url) || j.share_url || null;
              }
              // Metadata key-value pairs live under repeated field 9.
              let clientMsgId = null, isStranger = null;
              for (const kv of _pbFindAll(eF, 9, 2)) {
                const kvF = _pbDecode(kv);
                const k = _bufToUtf8(_pbFindFirst(kvF, 1, 2) || new Uint8Array());
                const v = _bufToUtf8(_pbFindFirst(kvF, 2, 2) || new Uint8Array());
                if (k === "s:client_message_id") clientMsgId = v || null;
                else if (k === "s:is_stranger") isStranger = (v === "true");
              }
              if (mid === undefined || !entryCid) continue;
              messages.push({
                message_id: _bigToStr(mid),
                conversation_id: entryCid,
                sender_uid: senderUid !== undefined ? _bigToStr(senderUid) : null,
                sender_secuid: secUidBuf ? _bufToUtf8(secUidBuf) : null,
                text: contentJson && typeof contentJson === "object"
                  ? (contentJson.text || null) : null,
                aweType: contentJson && typeof contentJson === "object"
                  ? (typeof contentJson.aweType === "number" ? contentJson.aweType : null) : null,
                message_type: msgType !== undefined ? Number(msgType) : null,
                create_time_ms: createTimeMs !== undefined ? _bigToStr(createTimeMs) : null,
                client_message_id: clientMsgId,
                is_stranger: isStranger,
                media_url: mediaUrl,
                raw_content: contentJson,
              });
              if (entryCid && !seenThreads.has(entryCid)) {
                seenThreads.add(entryCid);
                threads.push({
                  conversation_id: entryCid,
                  conversation_type: convType !== undefined ? Number(convType) : null,
                  last_activity_ms: createTimeMs !== undefined ? _bigToStr(createTimeMs) : null,
                });
              }
            }
          }
          if (!messages.length) return null;
          return { threads, messages };
        } catch (e) {
          return null;
        }
      }

      // Owner UID lives in the WS URL as `device_id=<uid>`. Cached per socket.
      function _extractOwner(url) {
        try {
          const m = String(url || "").match(/[?&]device_id=(\d+)/);
          return m ? m[1] : "";
        } catch (e) { return ""; }
      }

      // IG DM decoder — scaffold. Real implementation needs MQTT frame
      // parsing (protocol level: CONNECT/CONNACK/PUBLISH/etc.) followed by
      // Thrift body decode for the PUBLISH payloads that carry inbox
      // events. Kept as a stub returning null so the WS-hook pipeline is
      // plumbed end-to-end for IG the moment the decoder implementation
      // lands. Reference: github.com/mautrix/meta messagix/mqtt for the
      // canonical modern IG MQTT dialect + Thrift schemas.
      //
      // Empirical observations (from 10 edge-chat sessions probed so far):
      //   * every observed frame is ≤ 4B — CONNACK (0x20 0x02 0x00 0x00) +
      //     PINGRESP (0xD0 0x00). No PUBLISH frames >= 24B have been seen.
      //   * strong hypothesis: IG's web client fetches actual DM content
      //     via /api/graphql/ or /graphql/query/ rather than pushing over
      //     the MQTT edge-chat socket. See harvestIGGraphQL below.
      function _igDecode(_buf) {
        // Intentional no-op. When ready to implement, expected return shape
        // is { threads: [{thread_id, title, participants, last_activity_ms}],
        //     messages: [{message_id, thread_id, sender_id, sender_username,
        //     text, item_type, timestamp_ms, is_from_me}] } — matches the
        // _upsert_ig_decoded shape in src/bridges/ig_ingest.py.
        return null;
      }
      // IG owner UID lives in the ds_user_id cookie, NOT the WS URL query
      // string. Read via document.cookie in the page context.
      function _extractOwnerIG() {
        try {
          const m = (document.cookie || "").match(/(?:^|;\s*)ds_user_id=(\d+)/);
          return m ? m[1] : "";
        } catch (e) { return ""; }
      }
      const WrappedWS = function (url, protocols) {
        const ws = protocols !== undefined ? new OrigWS(url, protocols) : new OrigWS(url);
        try {
          const u = String(url || "");
          const key = u.split("?")[0];
          const isDm = _dmSockRe.test(u);
          ws.addEventListener("message", function (ev) {
            try {
              const d = ev.data;
              if (typeof d === "string") {
                let j; try { j = JSON.parse(d); } catch (e) { return; }
                window.postMessage({ __uc: true, type: platform + "_dm", platform, frame: j }, "*");
                return;
              }
              // binary frame
              if (!_wsProbed.has(key)) {
                _wsProbed.add(key);
                _hookProbes++;
                const kind = (d instanceof ArrayBuffer) ? "arraybuffer"
                  : (typeof Blob !== "undefined" && d instanceof Blob) ? "blob" : typeof d;
                const size = (d && d.byteLength) || (d && d.size) || null;
                window.postMessage({ __uc: true, type: "dm_probe", platform, transport: "ws",
                  url: u.slice(0, 200), frame_kind: kind, frame_size: size }, "*");
              }
              // raw sample of substantial frames on the real DM sockets
              if (isDm) {
                if (d instanceof ArrayBuffer) {
                  shipSample(u, key, d);
                  // Option B: client-side decode → structured DM payload.
                  // TikTok uses protobuf on the frontier socket; IG uses
                  // MQTT-over-WSS + Thrift on edge-chat (decoder stubbed
                  // for now, see _igDecode).
                  if (platform === "tiktok" && d.byteLength >= SAMPLE_MIN_BYTES) {
                    try {
                      const decoded = _ttDecode(d);
                      if (decoded) {
                        window.postMessage({
                          __uc: true, type: "dm_decoded", platform,
                          owner: _extractOwner(u),
                          threads: decoded.threads, messages: decoded.messages,
                        }, "*");
                      }
                    } catch (e) {}
                  } else if (platform === "instagram" && d.byteLength >= IG_SAMPLE_MIN_BYTES) {
                    try {
                      const decoded = _igDecode(d);
                      if (decoded) {
                        window.postMessage({
                          __uc: true, type: "dm_decoded", platform,
                          owner: _extractOwnerIG(),
                          threads: decoded.threads, messages: decoded.messages,
                        }, "*");
                      }
                    } catch (e) {}
                  }
                }
                else if (typeof Blob !== "undefined" && d instanceof Blob) {
                  const fr = new FileReader();
                  fr.onload = function () {
                    try {
                      shipSample(u, key, fr.result);
                      if (platform === "tiktok" && fr.result.byteLength >= SAMPLE_MIN_BYTES) {
                        const decoded = _ttDecode(fr.result);
                        if (decoded) {
                          window.postMessage({
                            __uc: true, type: "dm_decoded", platform,
                            owner: _extractOwner(u),
                            threads: decoded.threads, messages: decoded.messages,
                          }, "*");
                        }
                      } else if (platform === "instagram" && fr.result.byteLength >= IG_SAMPLE_MIN_BYTES) {
                        const decoded = _igDecode(fr.result);
                        if (decoded) {
                          window.postMessage({
                            __uc: true, type: "dm_decoded", platform,
                            owner: _extractOwnerIG(),
                            threads: decoded.threads, messages: decoded.messages,
                          }, "*");
                        }
                      }
                    } catch (e) {}
                  };
                  fr.readAsArrayBuffer(d);
                }
              }
            } catch (e) {}
          });
        } catch (e) {}
        return ws;
      };
      WrappedWS.prototype = OrigWS.prototype;
      WrappedWS.CONNECTING = OrigWS.CONNECTING; WrappedWS.OPEN = OrigWS.OPEN;
      WrappedWS.CLOSING = OrigWS.CLOSING; WrappedWS.CLOSED = OrigWS.CLOSED;
      window.WebSocket = WrappedWS;

      // P1.3: WS-hook heartbeat. Every 5 min the running counters are shipped
      // to /social/dm-heartbeat via content.js -> background.js so the
      // watchdog can tell "extension not installed" from "hook silently
      // broken by an IG/TikTok bundle update". Sending happens even when
      // counters are still 0 — "hook loaded and alive but no traffic" is a
      // valid state that the dashboard should surface.
      try {
        const HEARTBEAT_MIN = 5;
        setInterval(function () {
          try {
            window.postMessage({
              __uc: true, type: "dm_heartbeat", platform,
              probes_sent: _hookProbes, samples_shipped: _hookSamples,
            }, "*");
          } catch (e) {}
        }, HEARTBEAT_MIN * 60 * 1000);
        // Fire one immediately after install so a fresh page load shows up
        // in the dashboard without waiting 5 min for the first tick.
        setTimeout(function () {
          try {
            window.postMessage({
              __uc: true, type: "dm_heartbeat", platform,
              probes_sent: _hookProbes, samples_shipped: _hookSamples,
            }, "*");
          } catch (e) {}
        }, 15 * 1000);
      } catch (e) {}
    } catch (e) {}
  }
})();
