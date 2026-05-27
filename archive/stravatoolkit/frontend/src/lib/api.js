export async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();

  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const detail =
      (payload && (payload.detail || payload.message || payload.error)) ||
      text ||
      `Request failed with HTTP ${response.status}`;
    throw new Error(detail);
  }

  if (payload === null) {
    throw new Error(`Expected JSON from ${url}, but received invalid content.`);
  }

  return payload;
}
