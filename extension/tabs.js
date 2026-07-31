const $ = (id) => document.getElementById(id);

function badge(label, state, text) {
  // state: ok | bad | warn | idle
  return `<span class="badge"><span class="dot ${state}"></span>${text}</span>`;
}

async function render() {
  let platforms = [];
  try { platforms = (await chrome.runtime.sendMessage({ type: "getPlatforms" })) || []; } catch (e) {}
  const list = $("list");
  list.innerHTML = platforms
    .map((p) => {
      const login = p.noLogin ? badge("login", "idle", "no login needed")
        : p.loggedIn === true ? badge("login", "ok", "logged in")
        : p.loggedIn === false ? badge("login", "bad", "not logged in")
        : badge("login", "idle", "login unknown");
      const tab = p.tabOpen ? badge("tab", "ok", "tab open") : badge("tab", "idle", "no tab");
      const scr = p.scraper
        ? `<span class="pill active">scraper active</span>`
        : `<span class="pill">login-ready</span>`;
      return `
        <div class="card">
          <div>
            <div class="name">${p.label} ${scr}</div>
            <div class="host">${p.host}</div>
          </div>
          <div class="badges">
            ${tab}
            ${login}
            <button class="open" data-id="${p.id}">${p.tabOpen ? "Focus" : "Open"}</button>
          </div>
        </div>`;
    })
    .join("");

  list.querySelectorAll("button.open").forEach((b) => {
    b.addEventListener("click", async () => {
      await chrome.runtime.sendMessage({ type: "openPlatform", id: b.dataset.id });
      setTimeout(render, 400);
    });
  });

  const scrapers = platforms.filter((p) => p.scraper).map((p) => p.label).join(", ") || "none yet";
  $("note").innerHTML =
    `<b>Scrapers active:</b> ${scrapers}. Other platforms open for login now and will scrape ` +
    `automatically once their scraper is added. Tabs open <b>pinned</b> so they persist; ` +
    `keep them logged in. The bridge runs a scrape cycle on a timer (see the popup).`;
}

$("openAll").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "openAll" });
  setTimeout(render, 800);
});
$("refresh").addEventListener("click", render);
$("scrape").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "scrapeNow" });
});
$("reloadExtension").addEventListener("click", reloadExtension);

async function reloadExtension() {
  try {
    await chrome.runtime.sendMessage({ type: "log", level: "info", msg: "manual extension reload requested" });
  } catch (e) {}
  try {
    await chrome.storage.local.set({ ucReloadIntent: { requested_at: Date.now(), source: "tabs_page" } });
  } catch (e) {}
  try {
    history.replaceState(null, "", location.pathname);
  } catch (e) {}
  chrome.runtime.reload();
}

try {
  if (new URL(location.href).searchParams.get("reload") === "1") {
    setTimeout(reloadExtension, 600);
  }
} catch (e) {}

render();
setInterval(render, 3000);
