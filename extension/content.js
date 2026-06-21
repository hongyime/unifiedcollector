// UnifiedCollector IG Bridge — content script.
// Runs on instagram.com, so fetches to IG's internal API are SAME-ORIGIN and
// automatically carry your logged-in session cookies. We add the headers IG
// requires (x-ig-app-id, x-csrftoken) and paginate a target profile's media,
// then hand the results to the background worker to forward to the collector.
//
// NOTE: IG rotates internal endpoints/params periodically. If web_profile_info /
// feed/user stop returning data, capture the current calls from DevTools
// (Network -> Fetch/XHR while scrolling a profile) and update the URLs below.

const IG_APP_ID = "936619743392459";

function csrfToken() {
  const m = document.cookie.match(/csrftoken=([^;]+)/);
  return m ? m[1] : "";
}

function igHeaders() {
  return {
    "x-ig-app-id": IG_APP_ID,
    "x-csrftoken": csrfToken(),
    "x-requested-with": "XMLHttpRequest",
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitter = (base) => base + Math.random() * base; // human-ish pacing

async function getProfile(username) {
  const url =
    "https://www.instagram.com/api/v1/users/web_profile_info/?username=" +
    encodeURIComponent(username);
  const res = await fetch(url, { headers: igHeaders(), credentials: "include" });
  if (!res.ok) throw new Error("web_profile_info " + res.status);
  const j = await res.json();
  return j && j.data && j.data.user;
}

// Pull the best media URL(s) out of a post node (handles carousels + video).
function extractMedia(node, username) {
  const out = [];
  const push = (n, cid) => {
    let url = null;
    let type = "photo";
    if (n.video_url) {
      url = n.video_url;
      type = "video";
    } else if (n.video_versions && n.video_versions[0]) {
      url = n.video_versions[0].url;
      type = "video";
    } else if (n.display_url) {
      url = n.display_url;
    } else if (
      n.image_versions2 &&
      n.image_versions2.candidates &&
      n.image_versions2.candidates[0]
    ) {
      url = n.image_versions2.candidates[0].url;
    }
    if (url) out.push({ content_id: String(cid), content_type: type, url, entity_name: username });
  };

  const cid = node.id || node.pk || node.code;
  const children =
    (node.edge_sidecar_to_children && node.edge_sidecar_to_children.edges) ||
    node.carousel_media;
  if (children && children.length) {
    children.forEach((c, i) => push(c.node || c, cid + "_" + i));
  } else {
    push(node, cid);
  }
  return out;
}

// Don't scrape/crawl celebrities — we want your network, not natgeo. Matches the
// server-side INSTA_SPIDER_FAMOUS_CAP default.
const SPIDER_FAMOUS_CAP = 100000;
const SPIDER_FOLLOWS_PER_SIDE = 150; // how many followers + following to harvest

// Pull a profile's posts from an already-fetched user object (avoids re-fetching).
async function scrapeUserMedia(user, username, maxItems = 300) {
  const media = [];
  const tl = user.edge_owner_to_timeline_media;
  if (tl && tl.edges) tl.edges.forEach((e) => media.push(...extractMedia(e.node, username)));

  let maxId = tl && tl.page_info && tl.page_info.end_cursor;
  let hasNext = tl && tl.page_info && tl.page_info.has_next_page;
  const userId = user.id;

  while (hasNext && media.length < maxItems) {
    await sleep(jitter(1500)); // pace pagination
    const url =
      "https://www.instagram.com/api/v1/feed/user/" +
      userId +
      "/?count=33" +
      (maxId ? "&max_id=" + maxId : "");
    let res;
    try {
      res = await fetch(url, { headers: igHeaders(), credentials: "include" });
    } catch (e) {
      break;
    }
    if (!res.ok) break;
    const j = await res.json();
    (j.items || []).forEach((it) => media.push(...extractMedia(it, username)));
    maxId = j.next_max_id;
    hasNext = j.more_available && !!maxId;
  }
  return media;
}

// Harvest a user's followers/following usernames (bounded) for the spider.
async function getFollows(userId, kind, max) {
  const out = [];
  let maxId = "";
  while (out.length < max) {
    const url =
      "https://www.instagram.com/api/v1/friendships/" +
      userId + "/" + kind + "/?count=50" +
      (maxId ? "&max_id=" + maxId : "");
    let res;
    try {
      res = await fetch(url, { headers: igHeaders(), credentials: "include" });
    } catch (e) {
      break;
    }
    if (!res.ok) break;
    const j = await res.json();
    (j.users || []).forEach((u) => {
      if (u && u.username) out.push({ username: u.username });
    });
    maxId = j.next_max_id;
    if (!maxId) break;
    await sleep(jitter(1500)); // pace graph pagination
  }
  return out.slice(0, max);
}

async function runCycle() {
  let resp = [];
  try {
    resp = (await chrome.runtime.sendMessage({ type: "getTargets" })) || [];
  } catch (e) {
    /* background not ready */
  }
  // Accept both the new [{username, hop}] shape and the old ["user", ...] one.
  const targets = (Array.isArray(resp) ? resp : []).map((t) =>
    typeof t === "string" ? { username: t, hop: 0 } : t
  );
  const MAX_HOP = 2;
  console.log("[IG Bridge] cycle: %d target(s)", targets.length);

  for (const t of targets) {
    const username = t.username;
    const hop = typeof t.hop === "number" ? t.hop : 0;
    if (!username) continue;
    try {
      const user = await getProfile(username);
      if (!user) continue;
      const followerCount =
        (user.edge_followed_by && user.edge_followed_by.count) || 0;
      if (followerCount > SPIDER_FAMOUS_CAP) {
        console.log("[IG Bridge] skip famous %s (%d followers)", username, followerCount);
        continue;
      }

      // 1) scrape this profile's media
      const media = await scrapeUserMedia(user, username);
      if (media.length) {
        await chrome.runtime.sendMessage({ type: "ingest", username, items: media });
        console.log("[IG Bridge] %s -> %d media", username, media.length);
      }

      // 2) spider: crawl the graph one more hop out (friends-of-friends)
      if (hop < MAX_HOP && user.id) {
        const a = await getFollows(user.id, "followers", SPIDER_FOLLOWS_PER_SIDE);
        const b = await getFollows(user.id, "following", SPIDER_FOLLOWS_PER_SIDE);
        const discovered = a.concat(b);
        if (discovered.length) {
          await chrome.runtime.sendMessage({ type: "discover", source: username, hop, discovered });
          console.log("[IG Bridge] %s (hop %d) -> discovered %d", username, hop, discovered.length);
        }
      }
    } catch (e) {
      console.warn("[IG Bridge] scrape failed", username, e.message);
    }
    await sleep(jitter(4000)); // pace between profiles (avoid behavioural flags)
  }
}

// Background alarm asks us to run a cycle (content scripts have the session).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "scrapeCycle") {
    runCycle().then(() => sendResponse({ ok: true }));
    return true; // async response
  }
});
