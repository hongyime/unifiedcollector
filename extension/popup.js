const $ = (id) => document.getElementById(id);

async function load() {
  const { ingestBase, intervalMinutes } = await chrome.storage.local.get([
    "ingestBase",
    "intervalMinutes",
  ]);
  $("ingest").value = ingestBase || "http://127.0.0.1:8765";
  $("interval").value = intervalMinutes || 30;
}

$("save").addEventListener("click", async () => {
  const ingestBase = $("ingest").value.trim() || "http://127.0.0.1:8765";
  const intervalMinutes = parseInt($("interval").value, 10) || 30;
  await chrome.storage.local.set({ ingestBase, intervalMinutes });
  chrome.alarms.create("ig-scrape", { periodInMinutes: intervalMinutes });
  $("status").textContent = "Saved.";
});

$("scrape").addEventListener("click", async () => {
  $("status").textContent = "Triggering scrape…";
  const res = await chrome.runtime.sendMessage({ type: "scrapeNow" });
  $("status").textContent = res && res.ok
    ? "Scraping (keep an instagram.com tab open)."
    : "No instagram.com tab open — open/pin one first.";
});

load();
