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

async function scrapeProfile(username, maxItems = 300) {
  const user = await getProfile(username);
  if (!user) return [];
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

async function runCycle() {
  let targets = [];
  try {
    targets = (await chrome.runtime.sendMessage({ type: "getTargets" })) || [];
  } catch (e) {
    /* background not ready */
  }
  console.log("[IG Bridge] cycle: %d target(s)", targets.length);
  for (const username of targets) {
    try {
      const media = await scrapeProfile(username);
      if (media.length) {
        await chrome.runtime.sendMessage({ type: "ingest", username, items: media });
        console.log("[IG Bridge] %s -> %d media", username, media.length);
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
