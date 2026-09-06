"use strict";
(() => {
  // src/inject.js
  (function() {
    const UC_INJECT_VERSION = "1.21.59";
    const existingHook = window.__UC_HOOKED__;
    if (existingHook && existingHook.version === UC_INJECT_VERSION) return;
    const HOST = location.hostname;
    const platform = /threads\.com$/.test(HOST) ? "threads" : /instagram\.com$/.test(HOST) ? "instagram" : /(^|\.)x\.com$/.test(HOST) || /twitter\.com$/.test(HOST) ? "x" : /facebook\.com$/.test(HOST) ? "facebook" : /tiktok\.com$/.test(HOST) ? "tiktok" : /strava\.com$/.test(HOST) ? "strava" : null;
    if (!platform) return;
    window.__UC_HOOKED__ = { version: UC_INJECT_VERSION, platform, installed_at: Date.now() };
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
    function emitMedia(username, items) {
      if (items && items.length) {
        window.postMessage({ __uc: true, type: "ingest", platform, username: username || "timeline", items }, "*");
      }
    }
    function followContextFromPath() {
      try {
        let owner = null, side = null;
        if (platform === "x") {
          const m = location.pathname.match(/^\/([A-Za-z0-9_]{1,20})\/(followers|following|verified_followers|following_verified)\/?$/i);
          if (m) {
            owner = m[1];
            side = /^follow/i.test(m[2]) && m[2] !== "followers" ? "follow" : "follower";
          }
        } else if (platform === "threads") {
          const m = location.pathname.match(/^\/@([A-Za-z0-9._]{1,30})\/(followers|following)\/?$/i);
          if (m) {
            owner = m[1];
            side = m[2].toLowerCase() === "following" ? "follow" : "follower";
          }
        }
        if (!owner || !side) return null;
        const stored = (localStorage.getItem("uc_owner_" + platform) || "").trim().replace(/^@/, "");
        if (stored && stored.toLowerCase() !== owner.toLowerCase()) return null;
        return { context: side, owner: { username: owner } };
      } catch (e) {
        return null;
      }
    }
    function _tiktokOwner() {
      try {
        let me = null;
        const uni = window.__UNIVERSAL_DATA_FOR_REHYDRATION__;
        if (uni && uni["__DEFAULT_SCOPE__"]) {
          const scope = uni["__DEFAULT_SCOPE__"];
          me = scope["webapp.app-context"] && scope["webapp.app-context"].user && scope["webapp.app-context"].user.uniqueId || scope["webapp.user-detail"] && scope["webapp.user-detail"].userInfo && scope["webapp.user-detail"].userInfo.user && scope["webapp.user-detail"].userInfo.user.uniqueId || null;
        }
        if (!me && window.SIGI_STATE && window.SIGI_STATE.AppContext && window.SIGI_STATE.AppContext.user) {
          me = window.SIGI_STATE.AppContext.user.uniqueId || null;
        }
        if (!me) return null;
        const viewed = (location.pathname.match(/^\/@([^/?#]+)/) || [])[1];
        if (viewed && viewed.toLowerCase() === String(me).toLowerCase()) return { username: me };
        return null;
      } catch (e) {
        return null;
      }
    }
    const _emittedTk = /* @__PURE__ */ new Set();
    function harvestTikTokList(url, text) {
      if (!text || text.length > 6e6) return;
      let json;
      try {
        json = JSON.parse(text);
      } catch (e) {
        return;
      }
      const list = json && (json.userList || json.user_list);
      if (!Array.isArray(list) || !list.length) return;
      let scene = null;
      try {
        scene = new URL(url, location.href).searchParams.get("scene");
      } catch (e) {
      }
      const context = scene === "67" ? "follower" : scene === "21" ? "follow" : "seen";
      const owner = context !== "seen" ? _tiktokOwner() : null;
      const users = [];
      for (const item of list) {
        const u = item && (item.user || item) || {};
        const uname = u.uniqueId || u.unique_id;
        if (!uname) continue;
        const id = u.id || u.uid || u.secUid;
        const k = context + ":" + (id || uname);
        if (_emittedTk.has(k)) continue;
        if (_emittedTk.size > 8e3) _emittedTk.clear();
        _emittedTk.add(k);
        users.push({
          user_id: id != null ? String(id) : null,
          username: uname,
          display_name: u.nickname || null,
          profile_pic_url: u.avatarThumb || u.avatarMedium || null
        });
      }
      if (users.length) emitUsers(users, context, owner);
    }
    function userFrom(o) {
      if (!o || typeof o !== "object") return null;
      const username = o.username || o.screen_name;
      const pk = o.pk || o.id || o.rest_id;
      if (!username || typeof username !== "string") return null;
      return {
        user_id: pk != null ? String(pk) : null,
        username,
        display_name: o.full_name || o.name || null,
        profile_pic_url: o.profile_pic_url || o.profile_image_url_https || null
      };
    }
    function postFrom(node) {
      if (!node || typeof node !== "object") return null;
      const pk = node.pk || node.id || node.code;
      const caption = node.caption && (node.caption.text || (typeof node.caption === "string" ? node.caption : null));
      const tpa = node.text_post_app_info || {};
      const likes = node.like_count;
      const replies = tpa.direct_reply_count ?? node.comment_count;
      const reposts = tpa.repost_count ?? tpa.reshare_count ?? node.reshare_count;
      if (!pk || likes === void 0 && caption == null && replies === void 0) return null;
      const user = node.user || {};
      return {
        platform_post_id: String(pk),
        author_username: user.username || null,
        caption: caption || null,
        likes_count: typeof likes === "number" ? likes : null,
        comments_count: typeof replies === "number" ? replies : null,
        reposts_count: typeof reposts === "number" ? reposts : null,
        taken_at: node.taken_at || node.taken_at_timestamp || null
      };
    }
    function tweetFrom(obj) {
      const lg = obj.legacy;
      if (!lg || lg.favorite_count === void 0) return null;
      const pid = obj.rest_id || lg.id_str;
      if (!pid) return null;
      let author = null;
      try {
        author = obj.core.user_results.result.legacy.screen_name;
      } catch (e) {
      }
      return {
        platform_post_id: String(pid),
        author_username: author,
        caption: lg.full_text || null,
        likes_count: lg.favorite_count ?? null,
        comments_count: lg.reply_count ?? null,
        reposts_count: lg.retweet_count ?? null,
        quote_count: lg.quote_count ?? null,
        views_count: (obj.views && (obj.views.count ? parseInt(obj.views.count, 10) : null)) ?? null,
        taken_at: lg.created_at ? Math.floor(Date.parse(lg.created_at) / 1e3) : null
      };
    }
    function _ucHash(s) {
      s = String(s || "");
      let h = 5381;
      for (let i = 0; i < s.length; i++) h = (h << 5) + h + s.charCodeAt(i) >>> 0;
      return h.toString(36);
    }
    function xMediaFromTweet(obj, tweet) {
      const items = [];
      if (platform !== "x" || !obj || !tweet || !tweet.platform_post_id) return items;
      const lg = obj.legacy || {};
      const media = [].concat(lg.extended_entities && lg.extended_entities.media || []).concat(lg.entities && lg.entities.media || []);
      const seen = /* @__PURE__ */ new Set();
      media.forEach((m, idx) => {
        const photo = m && (m.media_url_https || m.media_url);
        if (photo && /^https?:\/\/pbs\.twimg\.com\//i.test(photo) && !seen.has(photo)) {
          seen.add(photo);
          const url = (() => {
            try {
              const u = new URL(photo);
              if (/\/media\//i.test(u.pathname)) u.searchParams.set("name", "orig");
              return u.toString();
            } catch (e) {
              return photo;
            }
          })();
          items.push({
            content_id: `gql_img_${tweet.platform_post_id}_${idx}_${_ucHash(url)}`,
            content_type: "photo",
            url,
            entity_name: tweet.author_username || "timeline",
            kind: "post",
            meta: {
              x_asset_role: "graphql_image",
              post_id: tweet.platform_post_id,
              author_username: tweet.author_username || null,
              post_url: tweet.author_username ? `https://x.com/${tweet.author_username}/status/${tweet.platform_post_id}` : null
            }
          });
        }
        const variants = (m && m.video_info && m.video_info.variants || []).filter((v) => v && v.url && /video\/mp4/i.test(v.content_type || "") && /\.mp4(?:$|\?)/i.test(v.url)).sort((a, b) => Number(b.bitrate || 0) - Number(a.bitrate || 0));
        if (variants.length) {
          const v = variants[0];
          if (!seen.has(v.url)) {
            seen.add(v.url);
            items.push({
              content_id: `gql_vid_${tweet.platform_post_id}_${idx}_${Number(v.bitrate || 0)}_${_ucHash(v.url)}`,
              content_type: "video",
              url: v.url,
              entity_name: tweet.author_username || "timeline",
              kind: "post",
              browser_upload: true,
              browser_upload_only: true,
              meta: {
                x_asset_role: "graphql_video_mp4",
                post_id: tweet.platform_post_id,
                author_username: tweet.author_username || null,
                bitrate: v.bitrate || null,
                post_url: tweet.author_username ? `https://x.com/${tweet.author_username}/status/${tweet.platform_post_id}` : null
              }
            });
          }
        }
      });
      return items;
    }
    function scan(obj, out, users, media, depth) {
      if (!obj || depth > 9 || out.length > 400 && users.length > 1500) return;
      if (Array.isArray(obj)) {
        for (const v of obj) scan(v, out, users, media, depth + 1);
        return;
      }
      if (typeof obj !== "object") return;
      if (platform === "x" && obj.legacy && obj.legacy.favorite_count !== void 0) {
        const t = tweetFrom(obj);
        if (t) {
          out.push(t);
          xMediaFromTweet(obj, t).forEach((item) => media.push(item));
        }
      } else if (platform === "facebook" && obj.reaction_count && typeof obj.reaction_count.count === "number") {
        const pid = obj.subscription_target_id || obj.associated_group_id || obj.share_fbid || obj.id;
        if (pid) {
          const comments = obj.total_comment_count != null ? obj.total_comment_count : obj.comment_rendering_instance && obj.comment_rendering_instance.comments && obj.comment_rendering_instance.comments.total_count;
          out.push({
            platform_post_id: String(pid),
            likes_count: obj.reaction_count.count,
            comments_count: typeof comments === "number" ? comments : null,
            shares_count: (obj.share_count && obj.share_count.count) ?? (obj.reshare_count && obj.reshare_count.count) ?? null
          });
        }
      } else if (obj.like_count !== void 0 || obj.text_post_app_info || obj.caption && obj.pk) {
        const p = postFrom(obj);
        if (p) out.push(p);
      }
      if ((obj.username || obj.screen_name) && (obj.pk !== void 0 || obj.id !== void 0 || obj.rest_id !== void 0)) {
        const u = userFrom(obj);
        if (u) users.push(u);
      }
      for (const k in obj) {
        const v = obj[k];
        if (v && typeof v === "object") scan(v, out, users, media, depth + 1);
      }
    }
    const _emittedP = /* @__PURE__ */ new Set(), _emittedU = /* @__PURE__ */ new Set(), _emittedM = /* @__PURE__ */ new Set();
    const _cap = (s) => {
      if (s.size > 5e3) s.clear();
    };
    function harvestText(text) {
      if (!text || text.length > 6e6) return;
      let json;
      try {
        json = JSON.parse(text);
      } catch (e) {
        return;
      }
      const out = [], users = [], media = [];
      scan(json, out, users, media, 0);
      _cap(_emittedP);
      _cap(_emittedU);
      _cap(_emittedM);
      emit(out.filter((p) => _emittedP.has(p.platform_post_id) ? false : _emittedP.add(p.platform_post_id)));
      emitMedia("timeline", media.filter((item) => {
        const k = item.content_id || item.url;
        return _emittedM.has(k) ? false : _emittedM.add(k);
      }));
      const rel = followContextFromPath();
      emitUsers(
        users.filter((u) => {
          const k = u.user_id || u.username;
          return _emittedU.has(k) ? false : _emittedU.add(k);
        }),
        rel && rel.context,
        rel && rel.owner
      );
    }
    const apiRe = /graphql|\/api\/v1\//;
    const tkListRe = /\/api\/user\/list\//;
    const dmRe = /\/direct_v2\/(inbox|threads|thread)/;
    const stravaStreamsRe = /\/(?:api\/v3\/)?activities\/(\d+)\/streams(?:[?#/]|$)/;
    const _probedUrls = /* @__PURE__ */ new Set();
    function probeDmUrl(url) {
      try {
        if (!/direct/i.test(url)) return;
        const key = String(url).split("?")[0];
        if (_probedUrls.has(key)) return;
        _probedUrls.add(key);
        window.postMessage({ __uc: true, type: "dm_probe", platform, transport: "http", url: key.slice(0, 300) }, "*");
      } catch (e) {
      }
    }
    function harvestDMs(text) {
      if (!text || text.length > 8e6) return;
      let json;
      try {
        json = JSON.parse(text);
      } catch (e) {
        return;
      }
      let threads = [];
      if (json.inbox && Array.isArray(json.inbox.threads)) threads = json.inbox.threads;
      else if (json.thread) threads = [json.thread];
      else if (Array.isArray(json.threads)) threads = json.threads;
      if (!threads.length) return;
      let ownerId = null;
      try {
        ownerId = (document.cookie.match(/ds_user_id=(\d+)/) || [])[1] || null;
      } catch (e) {
      }
      window.postMessage({ __uc: true, type: "dms", platform: "instagram", owner: { id: ownerId }, threads }, "*");
    }
    const _emittedStravaStreams = /* @__PURE__ */ new Set();
    function _streamData(container, key) {
      try {
        if (!container) return [];
        if (Array.isArray(container)) {
          for (const item of container) {
            if (!item || typeof item !== "object") continue;
            if (item.type === key || item.name === key || item.key === key) {
              return Array.isArray(item.data) ? item.data : [];
            }
          }
          return [];
        }
        if (typeof container === "object") {
          const raw = container[key];
          if (Array.isArray(raw)) return raw;
          if (raw && typeof raw === "object" && Array.isArray(raw.data)) return raw.data;
        }
      } catch (e) {
      }
      return [];
    }
    function harvestStravaStreams(url, text, status) {
      try {
        if (platform !== "strava" || !text || text.length > 12e6) return;
        const m = String(url || "").match(stravaStreamsRe);
        const activityId = m && m[1];
        if (!activityId) return;
        const statusCode = Number(status || 0);
        if (statusCode === 429 || statusCode === 401 || statusCode === 403) {
          const statusKey = activityId + ":http:" + statusCode;
          if (_emittedStravaStreams.has(statusKey)) return;
          if (_emittedStravaStreams.size > 1e3) _emittedStravaStreams.clear();
          _emittedStravaStreams.add(statusKey);
          window.postMessage({
            __uc: true,
            type: "strava_streams",
            platform: "strava",
            activity_id: activityId,
            request_url: String(url).slice(0, 500),
            http_status: statusCode,
            point_count: 0,
            streams: {}
          }, "*");
          return;
        }
        let json;
        try {
          json = JSON.parse(text);
        } catch (e) {
          return;
        }
        const latlng = _streamData(json, "latlng");
        if (!Array.isArray(latlng) || latlng.length < 2) return;
        const key = activityId + ":" + latlng.length;
        if (_emittedStravaStreams.has(key)) return;
        if (_emittedStravaStreams.size > 1e3) _emittedStravaStreams.clear();
        _emittedStravaStreams.add(key);
        window.postMessage({
          __uc: true,
          type: "strava_streams",
          platform: "strava",
          activity_id: activityId,
          request_url: String(url).slice(0, 500),
          http_status: status || null,
          point_count: latlng.length,
          streams: {
            latlng,
            time: _streamData(json, "time"),
            altitude: _streamData(json, "altitude"),
            distance: _streamData(json, "distance"),
            heartrate: _streamData(json, "heartrate"),
            cadence: _streamData(json, "cadence"),
            watts: _streamData(json, "watts"),
            speed: _streamData(json, "speed"),
            grade_smooth: _streamData(json, "grade_smooth")
          }
        }, "*");
      } catch (e) {
      }
    }
    const igGraphQLRe = /\/(?:api\/graphql|graphql\/query|ajax\/route-definition|ajax\/bulk-route-definitions)/;
    function harvestIGGraphQL(url, text) {
      try {
        if (!text || text.length > 8e6) return;
        if (!/"thread_id"|"direct_message"|"conversation_id"|"inbox"/.test(text)) return;
        let json;
        try {
          json = JSON.parse(text);
        } catch (e) {
          return;
        }
        window.postMessage({
          __uc: true,
          type: "dm_probe",
          platform: "instagram",
          transport: "graphql",
          url: url.slice(0, 300),
          frame_kind: "json",
          frame_size: text.length
        }, "*");
        let threads = null;
        const roots = [
          json,
          json && json.data,
          json && json.viewer,
          json && json.data && json.data.viewer
        ];
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
        try {
          ownerId = (document.cookie.match(/ds_user_id=(\d+)/) || [])[1] || null;
        } catch (e) {
        }
        window.postMessage({
          __uc: true,
          type: "dms",
          platform: "instagram",
          owner: { id: ownerId },
          threads
        }, "*");
      } catch (e) {
      }
    }
    const origFetch = window.fetch;
    window.fetch = function(...args) {
      const p = origFetch.apply(this, args);
      try {
        const url = String(args[0] && (args[0].url || args[0]) || "");
        if (platform === "tiktok" && tkListRe.test(url)) {
          p.then((r) => {
            try {
              r.clone().text().then((t) => harvestTikTokList(url, t)).catch(() => {
              });
            } catch (e) {
            }
          }).catch(() => {
          });
        } else if (platform === "instagram" && dmRe.test(url)) {
          p.then((r) => {
            try {
              r.clone().text().then(harvestDMs).catch(() => {
              });
            } catch (e) {
            }
          }).catch(() => {
          });
        } else if (platform === "instagram" && /direct/i.test(url)) {
          probeDmUrl(url);
          p.then((r) => {
            try {
              r.clone().text().then(harvestDMs).catch(() => {
              });
            } catch (e) {
            }
          }).catch(() => {
          });
        } else if (platform === "instagram" && igGraphQLRe.test(url)) {
          p.then((r) => {
            try {
              r.clone().text().then((t) => harvestIGGraphQL(url, t)).catch(() => {
              });
            } catch (e) {
            }
          }).catch(() => {
          });
        } else if (platform === "strava" && stravaStreamsRe.test(url)) {
          p.then((r) => {
            try {
              r.clone().text().then((t) => harvestStravaStreams(url, t, r.status)).catch(() => {
              });
            } catch (e) {
            }
          }).catch(() => {
          });
        } else if (apiRe.test(url)) {
          p.then((r) => {
            try {
              r.clone().text().then(harvestText).catch(() => {
              });
            } catch (e) {
            }
          }).catch(() => {
          });
        }
      } catch (e) {
      }
      return p;
    };
    const OrigOpen = XMLHttpRequest.prototype.open;
    const OrigSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
      this.__uc_url = url;
      return OrigOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
      try {
        const url = String(this.__uc_url || "");
        if (platform === "tiktok" && tkListRe.test(url)) {
          this.addEventListener("load", function() {
            try {
              if (typeof this.responseText === "string") harvestTikTokList(url, this.responseText);
            } catch (e) {
            }
          });
        } else if (platform === "instagram" && dmRe.test(url)) {
          this.addEventListener("load", function() {
            try {
              if (typeof this.responseText === "string") harvestDMs(this.responseText);
            } catch (e) {
            }
          });
        } else if (platform === "instagram" && /direct/i.test(url)) {
          probeDmUrl(url);
          this.addEventListener("load", function() {
            try {
              if (typeof this.responseText === "string") harvestDMs(this.responseText);
            } catch (e) {
            }
          });
        } else if (platform === "instagram" && igGraphQLRe.test(url)) {
          this.addEventListener("load", function() {
            try {
              if (typeof this.responseText === "string") harvestIGGraphQL(url, this.responseText);
            } catch (e) {
            }
          });
        } else if (platform === "strava" && stravaStreamsRe.test(url)) {
          this.addEventListener("load", function() {
            try {
              if (typeof this.responseText === "string") harvestStravaStreams(url, this.responseText, this.status);
            } catch (e) {
            }
          });
        } else if (apiRe.test(url)) {
          this.addEventListener("load", function() {
            try {
              if (typeof this.responseText === "string") harvestText(this.responseText);
            } catch (e) {
            }
          });
        }
      } catch (e) {
      }
      return OrigSend.apply(this, arguments);
    };
    if (platform === "tiktok" || platform === "instagram") {
      let _postDmHeartbeat2 = function() {
        try {
          window.postMessage({
            __uc: true,
            type: "dm_heartbeat",
            platform,
            probes_sent: _hookProbes,
            samples_shipped: _hookSamples
          }, "*");
        } catch (e) {
        }
      };
      var _postDmHeartbeat = _postDmHeartbeat2;
      let _hookProbes = 0;
      let _hookSamples = 0;
      try {
        const HEARTBEAT_MIN = 5;
        setInterval(_postDmHeartbeat2, HEARTBEAT_MIN * 60 * 1e3);
        setTimeout(_postDmHeartbeat2, 15 * 1e3);
      } catch (e) {
      }
      try {
        let abToB642 = function(buf) {
          let s = "";
          const b = new Uint8Array(buf);
          for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
          return btoa(s);
        }, shipSample2 = function(u, key, buf) {
          try {
            const n = _sampleCount.get(key) || 0;
            if (n >= SAMPLE_MAX || !buf) return;
            const minBytes = platform === "instagram" ? IG_SAMPLE_MIN_BYTES : SAMPLE_MIN_BYTES;
            if (buf.byteLength < minBytes) return;
            _sampleCount.set(key, n + 1);
            _hookSamples++;
            window.postMessage({
              __uc: true,
              type: "dm_sample",
              platform,
              url: u.slice(0, 200),
              size: buf.byteLength,
              b64: abToB642(buf)
            }, "*");
          } catch (e) {
          }
        }, _pbReadVarint2 = function(buf, i) {
          let result = 0n, shift = 0n;
          while (i < buf.length) {
            const b = buf[i++];
            result |= BigInt(b & 127) << shift;
            if (!(b & 128)) return [result, i];
            shift += 7n;
            if (shift > 63n) throw new Error("varint too long");
          }
          throw new Error("varint truncated");
        }, _pbDecode2 = function(buf) {
          const out = [];
          let i = 0;
          while (i < buf.length) {
            let tag, next;
            try {
              [tag, next] = _pbReadVarint2(buf, i);
            } catch (e) {
              break;
            }
            i = next;
            const fn = Number(tag >> 3n);
            const wire = Number(tag & 0x7n);
            if (fn === 0) break;
            if (wire === 0) {
              let v;
              try {
                [v, i] = _pbReadVarint2(buf, i);
              } catch (e) {
                break;
              }
              out.push([fn, 0, v]);
            } else if (wire === 2) {
              let len;
              try {
                [len, i] = _pbReadVarint2(buf, i);
              } catch (e) {
                break;
              }
              const L = Number(len);
              if (i + L > buf.length) break;
              out.push([fn, 2, buf.subarray(i, i + L)]);
              i += L;
            } else if (wire === 1) {
              if (i + 8 > buf.length) break;
              i += 8;
              out.push([fn, 1, null]);
            } else if (wire === 5) {
              if (i + 4 > buf.length) break;
              i += 4;
              out.push([fn, 5, null]);
            } else {
              break;
            }
          }
          return out;
        }, _pbFindFirst2 = function(fields, fnum, wire) {
          for (const f of fields) if (f[0] === fnum && f[1] === wire) return f[2];
          return void 0;
        }, _pbFindAll2 = function(fields, fnum, wire) {
          const out = [];
          for (const f of fields) if (f[0] === fnum && f[1] === wire) out.push(f[2]);
          return out;
        }, _bufToUtf82 = function(u8) {
          try {
            return new TextDecoder("utf-8", { fatal: false }).decode(u8);
          } catch (e) {
            return null;
          }
        }, _bigToStr2 = function(b) {
          return typeof b === "bigint" ? b.toString() : String(b);
        }, _ttDecode2 = function(buf) {
          try {
            const u8 = new Uint8Array(buf);
            const outer = _pbDecode2(u8);
            const method = _pbFindFirst2(outer, 3, 0);
            if (method === void 0 || Number(method) !== 5) return null;
            const inner = _pbFindFirst2(outer, 8, 2);
            if (!inner) return null;
            const innerFields = _pbDecode2(inner);
            const wrap = _pbFindFirst2(innerFields, 6, 2);
            if (!wrap) return null;
            const wrapFields = _pbDecode2(wrap);
            const envelopes = _pbFindAll2(wrapFields, 500, 2);
            if (!envelopes.length) return null;
            const threads = [];
            const messages = [];
            const seenThreads = /* @__PURE__ */ new Set();
            for (const env of envelopes) {
              const envF = _pbDecode2(env);
              const cid = _pbFindFirst2(envF, 2, 2);
              const conversationId = cid ? _bufToUtf82(cid) : null;
              const convType = _pbFindFirst2(envF, 3, 0);
              for (const entry of _pbFindAll2(envF, 5, 2)) {
                const eF = _pbDecode2(entry);
                const inCid = _pbFindFirst2(eF, 1, 2);
                const entryCid = inCid ? _bufToUtf82(inCid) : conversationId;
                const mid = _pbFindFirst2(eF, 3, 0);
                const msgType = _pbFindFirst2(eF, 6, 0);
                const senderUid = _pbFindFirst2(eF, 7, 0);
                const contentBuf = _pbFindFirst2(eF, 8, 2);
                const createTimeMs = _pbFindFirst2(eF, 10, 0);
                const secUidBuf = _pbFindFirst2(eF, 14, 2);
                const contentStr = contentBuf ? _bufToUtf82(contentBuf) : null;
                let contentJson = null;
                try {
                  contentJson = contentStr ? JSON.parse(contentStr) : null;
                } catch (e) {
                  contentJson = null;
                }
                if (contentJson && typeof contentJson === "object" && "command_type" in contentJson) continue;
                let mediaUrl = null;
                if (contentJson && typeof contentJson === "object") {
                  const j = contentJson;
                  const aw = typeof j.aweType === "number" ? j.aweType : -1;
                  if (aw === 1) mediaUrl = j.stickerUrl || j.imageInfo && (j.imageInfo.url || j.imageInfo.imageUri) || null;
                  else if (aw === 2) mediaUrl = j.imageUri || j.display_image || j.imageInfo && (j.imageInfo.imageUri || j.imageInfo.url) || null;
                  else if (aw === 3) mediaUrl = j.videoInfo && (j.videoInfo.playAddr || j.videoInfo.playUri || j.videoInfo.url) || j.videoUrl || null;
                  else if (aw === 5) mediaUrl = j.audioInfo && (j.audioInfo.playUrl || j.audioInfo.url) || j.audioUrl || null;
                  else if (aw === 6) mediaUrl = j.giphyUrl || j.gifUrl || j.gifInfo && (j.gifInfo.url || j.gifInfo.playAddr) || null;
                  else if (aw === 7) mediaUrl = j.item && j.item.share && j.item.share.share_url || j.share_url || null;
                }
                let clientMsgId = null, isStranger = null;
                for (const kv of _pbFindAll2(eF, 9, 2)) {
                  const kvF = _pbDecode2(kv);
                  const k = _bufToUtf82(_pbFindFirst2(kvF, 1, 2) || new Uint8Array());
                  const v = _bufToUtf82(_pbFindFirst2(kvF, 2, 2) || new Uint8Array());
                  if (k === "s:client_message_id") clientMsgId = v || null;
                  else if (k === "s:is_stranger") isStranger = v === "true";
                }
                if (mid === void 0 || !entryCid) continue;
                messages.push({
                  message_id: _bigToStr2(mid),
                  conversation_id: entryCid,
                  sender_uid: senderUid !== void 0 ? _bigToStr2(senderUid) : null,
                  sender_secuid: secUidBuf ? _bufToUtf82(secUidBuf) : null,
                  text: contentJson && typeof contentJson === "object" ? contentJson.text || null : null,
                  aweType: contentJson && typeof contentJson === "object" ? typeof contentJson.aweType === "number" ? contentJson.aweType : null : null,
                  message_type: msgType !== void 0 ? Number(msgType) : null,
                  create_time_ms: createTimeMs !== void 0 ? _bigToStr2(createTimeMs) : null,
                  client_message_id: clientMsgId,
                  is_stranger: isStranger,
                  media_url: mediaUrl,
                  raw_content: contentJson
                });
                if (entryCid && !seenThreads.has(entryCid)) {
                  seenThreads.add(entryCid);
                  threads.push({
                    conversation_id: entryCid,
                    conversation_type: convType !== void 0 ? Number(convType) : null,
                    last_activity_ms: createTimeMs !== void 0 ? _bigToStr2(createTimeMs) : null
                  });
                }
              }
            }
            if (!messages.length) return null;
            return { threads, messages };
          } catch (e) {
            return null;
          }
        }, _extractOwner2 = function(url) {
          try {
            const m = String(url || "").match(/[?&]device_id=(\d+)/);
            return m ? m[1] : "";
          } catch (e) {
            return "";
          }
        }, _igDecode2 = function(_buf) {
          return null;
        }, _extractOwnerIG2 = function() {
          try {
            const m = (document.cookie || "").match(/(?:^|;\s*)ds_user_id=(\d+)/);
            return m ? m[1] : "";
          } catch (e) {
            return "";
          }
        }, _frameKind2 = function(d) {
          try {
            if (d instanceof ArrayBuffer) return "arraybuffer";
            if (ArrayBuffer.isView && ArrayBuffer.isView(d)) return d.constructor && d.constructor.name || "arraybuffer_view";
            if (typeof Blob !== "undefined" && d instanceof Blob) return "blob";
          } catch (e) {
          }
          return typeof d;
        }, _frameSize2 = function(d) {
          try {
            if (d instanceof ArrayBuffer) return d.byteLength;
            if (ArrayBuffer.isView && ArrayBuffer.isView(d)) return d.byteLength;
            if (typeof Blob !== "undefined" && d instanceof Blob) return d.size;
          } catch (e) {
          }
          return null;
        }, _decodeAndPost2 = function(u, buf) {
          try {
            if (platform === "tiktok" && buf.byteLength >= SAMPLE_MIN_BYTES) {
              const decoded = _ttDecode2(buf);
              if (decoded) {
                window.postMessage({
                  __uc: true,
                  type: "dm_decoded",
                  platform,
                  owner: _extractOwner2(u),
                  threads: decoded.threads,
                  messages: decoded.messages
                }, "*");
              }
            } else if (platform === "instagram" && buf.byteLength >= IG_SAMPLE_MIN_BYTES) {
              const decoded = _igDecode2(buf);
              if (decoded) {
                window.postMessage({
                  __uc: true,
                  type: "dm_decoded",
                  platform,
                  owner: _extractOwnerIG2(),
                  threads: decoded.threads,
                  messages: decoded.messages
                }, "*");
              }
            }
          } catch (e) {
          }
        }, _handleWSFrame2 = function(u, key, isDm, d) {
          try {
            if (typeof d === "string") {
              let j;
              try {
                j = JSON.parse(d);
              } catch (e) {
                return;
              }
              window.postMessage({ __uc: true, type: platform + "_dm", platform, frame: j }, "*");
              return;
            }
            if (!_wsProbed.has(key)) {
              _wsProbed.add(key);
              _hookProbes++;
              window.postMessage({
                __uc: true,
                type: "dm_probe",
                platform,
                transport: "ws",
                url: u.slice(0, 200),
                frame_kind: _frameKind2(d),
                frame_size: _frameSize2(d)
              }, "*");
            }
            if (!isDm) return;
            let buf = null;
            if (d instanceof ArrayBuffer) {
              buf = d;
            } else if (ArrayBuffer.isView && ArrayBuffer.isView(d)) {
              buf = d.buffer.slice(d.byteOffset, d.byteOffset + d.byteLength);
            }
            if (buf) {
              shipSample2(u, key, buf);
              _decodeAndPost2(u, buf);
            } else if (typeof Blob !== "undefined" && d instanceof Blob) {
              const fr = new FileReader();
              fr.onload = function() {
                try {
                  const out = fr.result;
                  if (out instanceof ArrayBuffer) {
                    shipSample2(u, key, out);
                    _decodeAndPost2(u, out);
                  }
                } catch (e) {
                }
              };
              fr.readAsArrayBuffer(d);
            }
          } catch (e) {
          }
        }, _observeSocket2 = function(ws, url) {
          try {
            const addListener = _nativeAddEventListener || (ws && typeof ws.addEventListener === "function" ? ws.addEventListener : null);
            if (!ws || typeof addListener !== "function") return ws;
            if (_observedSockets) {
              if (_observedSockets.has(ws)) return ws;
              _observedSockets.add(ws);
            } else if (ws.__uc_ws_observed) {
              return ws;
            } else {
              try {
                Object.defineProperty(ws, "__uc_ws_observed", { value: true });
              } catch (e) {
              }
            }
            const u = String(url || "");
            const key = u.split("?")[0] || u;
            const isDm = _dmSockRe.test(u);
            const onMessage = function(ev) {
              try {
                _handleWSFrame2(u, key, isDm, ev && ev.data);
              } catch (e) {
              }
            };
            try {
              addListener.call(ws, "message", onMessage, false);
            } catch (e) {
              try {
                ws.addEventListener("message", onMessage, false);
              } catch (_e) {
              }
            }
          } catch (e) {
          }
          return ws;
        }, _copyWSConstants2 = function(target, source) {
          for (const name of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) {
            try {
              const desc = Object.getOwnPropertyDescriptor(source, name);
              if (desc) Object.defineProperty(target, name, desc);
              else target[name] = source[name];
            } catch (e) {
              try {
                target[name] = source[name];
              } catch (_e) {
              }
            }
          }
        }, _isWrappedWS2 = function(fn) {
          try {
            return _wrappedWSFns ? _wrappedWSFns.has(fn) : !!fn.__uc_ws_hook;
          } catch (e) {
            return false;
          }
        }, _markWrapped2 = function(fn) {
          try {
            if (_wrappedWSFns) _wrappedWSFns.add(fn);
            else Object.defineProperty(fn, "__uc_ws_hook", { value: true });
          } catch (e) {
          }
          return fn;
        }, _makeWrappedWS2 = function(NativeWS) {
          if (typeof NativeWS !== "function") return NativeWS;
          if (_isWrappedWS2(NativeWS)) return NativeWS;
          if (_wrappedConstructors && _wrappedConstructors.has(NativeWS)) {
            return _wrappedConstructors.get(NativeWS);
          }
          let WrappedWS = null;
          if (typeof Proxy === "function" && typeof Reflect === "object" && Reflect.construct) {
            WrappedWS = new Proxy(NativeWS, {
              construct(target, args, newTarget) {
                let ws;
                try {
                  ws = Reflect.construct(target, args, newTarget || target);
                } catch (e) {
                  ws = Reflect.construct(target, args);
                }
                return _observeSocket2(ws, args && args[0]);
              },
              apply(target, thisArg, args) {
                return Reflect.apply(target, thisArg, args);
              }
            });
          } else {
            WrappedWS = function WebSocket(url, protocols) {
              const ws = protocols !== void 0 ? new NativeWS(url, protocols) : new NativeWS(url);
              return _observeSocket2(ws, url);
            };
            try {
              WrappedWS.prototype = NativeWS.prototype;
            } catch (e) {
            }
            _copyWSConstants2(WrappedWS, NativeWS);
          }
          _markWrapped2(WrappedWS);
          try {
            Object.defineProperty(NativeWS.prototype, "constructor", {
              value: WrappedWS,
              writable: true,
              configurable: true
            });
          } catch (e) {
          }
          if (_wrappedConstructors) _wrappedConstructors.set(NativeWS, WrappedWS);
          return WrappedWS;
        }, _installWSWrapper2 = function() {
          try {
            const desc = Object.getOwnPropertyDescriptor(window, "WebSocket");
            if (desc && !desc.configurable) {
              try {
                window.WebSocket = _currentWrappedWS;
              } catch (_e) {
              }
              return;
            }
            Object.defineProperty(window, "WebSocket", {
              configurable: true,
              enumerable: desc ? !!desc.enumerable : _wsDesc ? !!_wsDesc.enumerable : true,
              get() {
                return _currentWrappedWS;
              },
              set(v) {
                try {
                  if (!v || v === _currentWrappedWS) return;
                  _currentWrappedWS = _makeWrappedWS2(v);
                } catch (e) {
                  _currentWrappedWS = v;
                }
              }
            });
          } catch (e) {
            try {
              window.WebSocket = _currentWrappedWS;
            } catch (_e) {
            }
          }
        }, _ensureWSWrapper2 = function() {
          try {
            const current = window.WebSocket;
            if (current && current !== _currentWrappedWS) {
              _currentWrappedWS = _makeWrappedWS2(current);
              _installWSWrapper2();
            }
          } catch (e) {
          }
        };
        var abToB64 = abToB642, shipSample = shipSample2, _pbReadVarint = _pbReadVarint2, _pbDecode = _pbDecode2, _pbFindFirst = _pbFindFirst2, _pbFindAll = _pbFindAll2, _bufToUtf8 = _bufToUtf82, _bigToStr = _bigToStr2, _ttDecode = _ttDecode2, _extractOwner = _extractOwner2, _igDecode = _igDecode2, _extractOwnerIG = _extractOwnerIG2, _frameKind = _frameKind2, _frameSize = _frameSize2, _decodeAndPost = _decodeAndPost2, _handleWSFrame = _handleWSFrame2, _observeSocket = _observeSocket2, _copyWSConstants = _copyWSConstants2, _isWrappedWS = _isWrappedWS2, _markWrapped = _markWrapped2, _makeWrappedWS = _makeWrappedWS2, _installWSWrapper = _installWSWrapper2, _ensureWSWrapper = _ensureWSWrapper2;
        const OrigWS = window.WebSocket;
        const _nativeAddEventListener = (() => {
          try {
            const add = window.EventTarget && window.EventTarget.prototype && window.EventTarget.prototype.addEventListener;
            return typeof add === "function" ? add : null;
          } catch (e) {
            return null;
          }
        })();
        const _wsProbed = /* @__PURE__ */ new Set();
        const _dmSockRe = /edge-chat\.instagram\.com\/chat|im-ws[^/]*\.tiktok\.com\/ws/i;
        const _sampleCount = /* @__PURE__ */ new Map();
        const SAMPLE_MAX = 200, SAMPLE_MIN_BYTES = 24;
        const IG_SAMPLE_MIN_BYTES = 8;
        const _observedSockets = typeof WeakSet !== "undefined" ? /* @__PURE__ */ new WeakSet() : null;
        const _wrappedConstructors = typeof WeakMap !== "undefined" ? /* @__PURE__ */ new WeakMap() : null;
        const _wrappedWSFns = typeof WeakSet !== "undefined" ? /* @__PURE__ */ new WeakSet() : null;
        const _wsDesc = Object.getOwnPropertyDescriptor(window, "WebSocket");
        let _currentWrappedWS = _makeWrappedWS2(OrigWS);
        _installWSWrapper2();
        try {
          setInterval(_ensureWSWrapper2, 2e3);
        } catch (e) {
        }
      } catch (e) {
      }
    }
  })();
})();
