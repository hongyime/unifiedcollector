/** Visible-page extractors for the public Pinterest, Snapchat, and Airbnb surfaces. */

export type NativeScrapePlatform = "pinterest" | "snapchat" | "airbnb";
export type NativeScrapeItem = Record<string, unknown> & { type: string };

function cleanText(value: unknown, limit = 1000): string | null {
  if (typeof value !== "string") return null;
  const text = value.replace(/\s+/g, " ").trim();
  return text ? text.slice(0, limit) : null;
}

function absoluteHttps(raw: string | null | undefined, pageUrl: string): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw, pageUrl);
    return url.protocol === "https:" ? url.href.slice(0, 2048) : null;
  } catch {
    return null;
  }
}

function stableId(prefix: string, value: string): string {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return `${prefix}_${(hash >>> 0).toString(16)}`;
}

function visibleText(node: Element | null, limit = 1200): string | null {
  if (!(node instanceof HTMLElement)) return null;
  return cleanText(node.innerText || node.textContent || "", limit);
}

function pinterestUsername(pageUrl: string): string | null {
  try {
    const first = new URL(pageUrl).pathname.split("/").filter(Boolean)[0] || "";
    return /^(pin|search|ideas|_|categories|today|login|signup)$/i.test(first) ? null : cleanText(first, 100);
  } catch {
    return null;
  }
}

function pinterestItems(doc: Document, pageUrl: string): NativeScrapeItem[] {
  const username = pinterestUsername(pageUrl);
  const boardParts = new URL(pageUrl).pathname.split("/").filter(Boolean);
  const boardPage = boardParts.length === 2 && !new Set(["pin", "search", "ideas", "_", "_created", "_saved", "login", "signup"])
    .has(boardParts[0].toLowerCase()) && !new Set(["_created", "_saved"]).has(boardParts[1].toLowerCase());
  const boardId = boardPage ? boardParts.slice(0, 2).join(":") : null;
  const pins: NativeScrapeItem[] = [];
  const seenPins = new Set<string>();
  for (const anchor of Array.from(doc.querySelectorAll<HTMLAnchorElement>('a[href*="/pin/"]'))) {
    const pinUrl = absoluteHttps(anchor.href, pageUrl);
    if (!pinUrl || !/^(?:www\.)?pinterest\.com$/i.test(new URL(pinUrl).hostname)) continue;
    const match = new URL(pinUrl).pathname.match(/\/pin\/([A-Za-z0-9_-]+)/i);
    if (!match || seenPins.has(match[1])) continue;
    seenPins.add(match[1]);
    const card = anchor.closest<HTMLElement>(
      '[data-test-id="pinWrapper"], [data-test-id="pin"], [role="listitem"], article',
    ) || anchor.parentElement;
    const image = card?.querySelector<HTMLImageElement>("img[src]") || null;
    const destination = Array.from(card?.querySelectorAll<HTMLAnchorElement>("a[href^='http']") || [])
      .map((link) => absoluteHttps(link.href, pageUrl))
      .find((url) => url && !/(^|\.)pinterest\.com$/i.test(new URL(url).hostname)) || null;
    const boardLink = Array.from(card?.querySelectorAll<HTMLAnchorElement>("a[href]") || [])
      .map((link) => absoluteHttps(link.href, pageUrl))
      .find((url) => {
        if (!url) return false;
        const parts = new URL(url).pathname.split("/").filter(Boolean);
        const reserved = new Set(["pin", "search", "ideas", "_", "_created", "_saved", "login", "signup"]);
        return parts.length === 2 && !reserved.has(parts[0].toLowerCase()) && !reserved.has(parts[1].toLowerCase());
      }) || null;
    pins.push({
      type: "pin",
      id: match[1],
      username,
      url: pinUrl,
      board_url: boardLink,
      board_id: boardLink ? new URL(boardLink).pathname.split("/").filter(Boolean).slice(0, 2).join(":") : null,
      title: cleanText(anchor.getAttribute("aria-label") || card?.querySelector("h1,h2,h3,[title]")?.textContent || image?.alt, 500),
      description: visibleText(card, 800),
      image_url: absoluteHttps(image?.currentSrc || image?.src, pageUrl),
      destination_url: destination,
      metadata: { page_path: new URL(pageUrl).pathname },
    });
    if (pins.length >= 40) break;
  }

  if (boardPage && pins.length < 40) {
    const cards = Array.from(doc.querySelectorAll<HTMLElement>(
      '[data-test-id*="pinWrapper" i], [data-test-id="pin"], [data-test-id*="pinRep" i], main article',
    ));
    for (const card of cards) {
      const anchor = card.querySelector<HTMLAnchorElement>('a[href*="/pin/"]');
      const pinUrl = absoluteHttps(anchor?.href, pageUrl);
      const hrefId = pinUrl ? new URL(pinUrl).pathname.match(/\/pin\/([A-Za-z0-9_-]+)/i)?.[1] : null;
      const image = card.querySelector<HTMLImageElement>("img[src]");
      const imageUrl = absoluteHttps(image?.currentSrc || image?.src, pageUrl);
      const title = cleanText(card.querySelector("h1,h2,h3,[title]")?.textContent || image?.alt, 500);
      if (!imageUrl && !title) continue;
      const id = card.getAttribute("data-pin-id") || card.getAttribute("data-id") || hrefId ||
        stableId("pin", `${pageUrl}|${imageUrl || ""}|${title || ""}`);
      if (seenPins.has(id)) continue;
      seenPins.add(id);
      const text = visibleText(card, 800);
      pins.push({
        type: "pin",
        id,
        username,
        board_id: boardId,
        board_url: pageUrl,
        title: title && title.toLowerCase() !== "pin" ? title : null,
        description: text && text.toLowerCase() !== "pin" ? text : null,
        image_url: imageUrl,
        destination_url: null,
        url: pinUrl || pageUrl,
        metadata: { page_path: new URL(pageUrl).pathname, permalink_available: !!pinUrl },
      });
      if (pins.length >= 40) break;
    }
  }

  const boards: NativeScrapeItem[] = [];
  const seenBoards = new Set<string>();
  const reserved = new Set(["pin", "search", "ideas", "_", "_created", "_saved", "categories", "today", "login", "signup", "explore"]);
  for (const anchor of Array.from(doc.querySelectorAll<HTMLAnchorElement>("a[href]"))) {
    const href = absoluteHttps(anchor.href, pageUrl);
    if (!href || !/(^|\.)pinterest\.com$/i.test(new URL(href).hostname)) continue;
    const parts = new URL(href).pathname.split("/").filter(Boolean);
    if (parts.length !== 2 || reserved.has(parts[0].toLowerCase()) || reserved.has(parts[1].toLowerCase())) continue;
    const id = parts.map((part) => decodeURIComponent(part)).join(":");
    if (seenBoards.has(id)) continue;
    seenBoards.add(id);
    const card = anchor.closest<HTMLElement>('[data-test-id*="board" i], [role="listitem"], article') || anchor;
    const countMatch = (visibleText(card, 500) || "").match(/([\d,]+)\s+pins?\b/i);
    boards.push({
      type: "board",
      id,
      username: parts[0],
      url: href,
      title: cleanText(anchor.getAttribute("aria-label") || anchor.innerText || anchor.textContent, 500),
      pin_count: countMatch ? Number(countMatch[1].replace(/,/g, "")) : null,
      metadata: { page_path: new URL(pageUrl).pathname },
    });
    if (boards.length >= 10) break;
  }
  return [...pins, ...boards].slice(0, 50);
}

function snapchatUsername(pageUrl: string): string | null {
  try {
    const pathname = new URL(pageUrl).pathname;
    const match = pathname.match(/^\/(?:add|profile)\/([^/]+)/i) || pathname.match(/^\/@([^/]+)/);
    return match ? cleanText(decodeURIComponent(match[1]), 100) : null;
  } catch {
    return null;
  }
}

function snapchatStoryItems(doc: Document, pageUrl: string): NativeScrapeItem[] {
  const parsed = new URL(pageUrl);
  if (!/^\/(?:add|profile)\//i.test(parsed.pathname) && !/^\/@[^/]+/.test(parsed.pathname) &&
      !/^\/discover(?:\/|$)/i.test(parsed.pathname)) return [];
  const username = snapchatUsername(pageUrl);
  const nodes = Array.from(doc.querySelectorAll<HTMLElement>(
    '[data-story-id], [data-testid*="story" i], a[href*="/story/"], a[href*="/p/"], ' +
    '[aria-label*="public story" i], button[aria-label*="Profile Picture" i], main [data-testid*="discover-card" i]',
  ));
  const stories: NativeScrapeItem[] = [];
  const seen = new Set<string>();
  for (const node of nodes) {
    const card = node.closest<HTMLElement>('[data-testid*="story" i], [data-testid*="discover" i], article, [role="listitem"]') ||
      node.parentElement || node;
    const media = card.querySelector<HTMLVideoElement>("video") || null;
    const image = card.querySelector<HTMLImageElement>("img[src]") || null;
    const storyAnchor = node instanceof HTMLAnchorElement && /\/(?:story|p)\//i.test(node.pathname)
      ? node
      : card.querySelector<HTMLAnchorElement>('a[href*="/story/"], a[href*="/p/"]');
    const profileAnchor = card.querySelector<HTMLAnchorElement>('a[href*="/@"], a[href*="/add/"]');
    const storyUrl = absoluteHttps(storyAnchor?.href, pageUrl) ||
      absoluteHttps(profileAnchor?.href, pageUrl) || pageUrl;
    const mediaUrl = absoluteHttps(media?.currentSrc, pageUrl) || absoluteHttps(media?.src, pageUrl) ||
      absoluteHttps(media?.querySelector<HTMLSourceElement>("source[src]")?.src, pageUrl) ||
      absoluteHttps(image?.currentSrc || image?.src, pageUrl);
    const cardText = visibleText(card, 500);
    const title = cleanText(node.getAttribute("aria-label") || cardText, 500);
    const linkedUsername = Array.from(card.querySelectorAll<HTMLAnchorElement>('a[href*="/@"], a[href*="/add/"]'))
      .map((link) => snapchatUsername(link.href))
      .find((value) => value) || null;
    const label = node.getAttribute("aria-label") || card.getAttribute("aria-label") || title || "";
    const labelUsername = label.match(/Profile Picture\s+([A-Za-z0-9._-]+)/i)?.[1] || null;
    const textUsername = (cardText || "").match(/^([A-Za-z0-9._-]{1,100})\s+\d+\s*(?:m|h|d|w)\s+ago\b/i)?.[1] || null;
    const rawId = node.getAttribute("data-story-id") || node.getAttribute("data-item-id") ||
      storyUrl.match(/\/(?:story|p)\/([^/?#]+)/i)?.[1] || "";
    const storyUsername = username || linkedUsername || labelUsername || textUsername;
    const canonicalStoryUrl = storyUrl === pageUrl && storyUsername
      ? `https://www.snapchat.com/@${encodeURIComponent(storyUsername)}`
      : storyUrl;
    const mediaPath = mediaUrl ? new URL(mediaUrl).pathname : "";
    const stableTitle = cleanText((title || "").replace(/\s+\d+\s*(?:m|h|d|w)\s+ago\s*$/i, ""), 500);
    const id = cleanText(rawId, 200) || stableId("story", mediaPath
      ? `${storyUsername || "public"}|${mediaPath}`
      : `${storyUsername || "public"}|${canonicalStoryUrl}|${stableTitle || ""}`);
    if (seen.has(id) || (!mediaUrl && !title)) continue;
    seen.add(id);
    const time = node.querySelector<HTMLTimeElement>("time[datetime]")?.dateTime || null;
    stories.push({
      type: "story",
      id,
      username: storyUsername,
      title,
      url: canonicalStoryUrl,
      media_url: mediaUrl,
      thumbnail_url: absoluteHttps(image?.currentSrc || image?.src, pageUrl) || absoluteHttps(media?.poster, pageUrl) ||
        absoluteHttps(media?.querySelector<HTMLSourceElement>("source[src]")?.src, pageUrl),
      posted_at: time,
      expires_at: null,
      metadata: { page_path: parsed.pathname, visible_public_story: true },
    });
    if (stories.length >= 25) break;
  }
  return stories;
}

function airbnbHostItem(
  doc: Document,
  pageUrl: string,
  container: Element,
  preferredAnchor?: HTMLAnchorElement | null,
): NativeScrapeItem | null {
  const anchor = preferredAnchor || (container instanceof HTMLAnchorElement && /\/users\/(?:show|profile)\//i.test(container.pathname)
    ? container
    : container.querySelector<HTMLAnchorElement>('a[href*="/users/show/"], a[href*="/users/profile/"]'));
  const hostUrl = absoluteHttps(anchor?.href, pageUrl);
  if (!hostUrl) return null;
  const parsed = new URL(hostUrl);
  const idMatch = parsed.pathname.match(/\/users\/(?:show|profile)\/([^/?#]+)/i);
  if (!idMatch) return null;
  const hostId = decodeURIComponent(idMatch[1]);
  const text = visibleText(container, 1500) || "";
  const pageHeadings = Array.from(doc.querySelectorAll("h1,h2,h3"));
  const hostHeadingIndex = pageHeadings.findIndex((heading) => /^meet your host$/i.test(cleanText(heading.textContent, 200) || ""));
  const hostNameHeading = hostHeadingIndex >= 0
    ? pageHeadings.slice(hostHeadingIndex + 1).find((heading) => cleanText(heading.textContent, 200))
    : null;
  const rating = text.match(/\b([0-5](?:\.\d{1,2})?)\s*(?:·|\()\s*[\d,]+\s+reviews?\b/i) ||
    text.match(/\b([0-5](?:\.\d{1,2})?)\s+out of 5\b/i);
  const reviews = text.match(/\b([\d,]+)\s+reviews?\b/i);
  const yearsHosting = text.match(/\b(\d{1,2}\s+years?\s+hosting)\b/i);
  const image = anchor?.querySelector<HTMLImageElement>("img[src]") || null;
  return {
    type: "host",
    id: hostId,
    url: hostUrl,
    display_name: cleanText(hostNameHeading?.textContent || anchor?.innerText || image?.alt, 300),
    avatar_url: absoluteHttps(image?.currentSrc || image?.src, pageUrl),
    rating: rating ? Number(rating[1]) : null,
    reviews_count: reviews ? Number(reviews[1].replace(/,/g, "")) : null,
    host_since: yearsHosting ? yearsHosting[1] : null,
    metadata: { page_path: new URL(pageUrl).pathname },
  };
}

function airbnbListingItem(doc: Document, pageUrl: string, anchor: HTMLAnchorElement, card: Element): NativeScrapeItem | null {
  const listingUrl = absoluteHttps(anchor.href, pageUrl);
  if (!listingUrl || !/(^|\.)airbnb\.(com|com\.sg|co\.uk|co\.kr)$/i.test(new URL(listingUrl).hostname)) return null;
  const match = new URL(listingUrl).pathname.match(/\/rooms\/(\d+)/i);
  if (!match) return null;
  const text = visibleText(card, 1800) || "";
  const titleNode = card.querySelector("h1,h2,h3,[data-testid*='title' i]");
  const image = card.querySelector<HTMLImageElement>("img[src]");
  const rating = text.match(/\b([0-5](?:\.\d)?)\s*(?:·|\()\s*[\d,]+\s+reviews?\b/i) || text.match(/\bRated\s+([0-5](?:\.\d)?)/i);
  const reviews = text.match(/\b([\d,]+)\s+reviews?\b/i);
  const bedrooms = text.match(/\b(\d+(?:\.\d+)?)\s+bedrooms?\b/i);
  const bathrooms = text.match(/\b(\d+(?:\.\d+)?)\s+bathrooms?\b/i);
  const guests = text.match(/\b(\d+)\s+guests?\b/i);
  const host = airbnbHostItem(doc, pageUrl, card);
  return {
    type: "listing",
    id: match[1],
    url: listingUrl,
    host_id: host?.id || null,
    host_name: host?.display_name || null,
    host_profile_url: host?.url || null,
    title: cleanText(titleNode?.textContent || anchor.getAttribute("aria-label") || image?.alt, 500),
    location: cleanText(card.querySelector('[data-testid*="location" i]')?.textContent || null, 500),
    image_url: absoluteHttps(image?.currentSrc || image?.src, pageUrl),
    rating: rating ? Number(rating[1]) : null,
    reviews_count: reviews ? Number(reviews[1].replace(/,/g, "")) : null,
    property_type: null,
    bedrooms: bedrooms ? Number(bedrooms[1]) : null,
    bathrooms: bathrooms ? Number(bathrooms[1]) : null,
    max_guests: guests ? Number(guests[1]) : null,
    metadata: { page_path: new URL(pageUrl).pathname },
  };
}

function airbnbItems(doc: Document, pageUrl: string): NativeScrapeItem[] {
  const parsed = new URL(pageUrl);
  const output: NativeScrapeItem[] = [];
  const seenListings = new Set<string>();
  const roomId = parsed.pathname.match(/\/rooms\/(\d+)/i)?.[1];
  if (roomId) {
    const main = doc.querySelector("main") || doc.body;
    const anchor = doc.querySelector<HTMLAnchorElement>(`a[href*="/rooms/${roomId}"]`) || document.createElement("a");
    if (!anchor.href) anchor.href = pageUrl;
    const item = airbnbListingItem(doc, pageUrl, anchor, main);
    if (item) {
      seenListings.add(String(item.id));
      output.push(item);
      const profileLink = Array.from(doc.querySelectorAll<HTMLAnchorElement>('a[href*="/users/show/"], a[href*="/users/profile/"]'))
        .find((candidate) => /host full profile/i.test(candidate.getAttribute("aria-label") || candidate.innerText || "")) || null;
      const hostContainer = profileLink?.closest("section") || profileLink?.parentElement?.parentElement || main;
      const host = airbnbHostItem(doc, pageUrl, hostContainer, profileLink) || airbnbHostItem(doc, pageUrl, main);
      if (host) output.push(host);
    }
  }
  if (/\/s\//i.test(parsed.pathname)) {
    for (const anchor of Array.from(doc.querySelectorAll<HTMLAnchorElement>('a[href*="/rooms/"]'))) {
      const card = anchor.closest<HTMLElement>('[data-testid="card-container"], [data-testid*="listing-card" i], article, li') || anchor.parentElement || anchor;
      const item = airbnbListingItem(doc, pageUrl, anchor, card);
      if (!item || seenListings.has(String(item.id))) continue;
      seenListings.add(String(item.id));
      output.push(item);
      if (output.length >= 30) break;
    }
  }
  if (/\/users\/(?:show|profile)\//i.test(parsed.pathname)) {
    const main = doc.querySelector("main") || doc.body;
    const id = parsed.pathname.match(/\/users\/(?:show|profile)\/([^/?#]+)/i)?.[1];
    if (id) {
      const text = visibleText(main, 1500) || "";
      const reviews = text.match(/\b([\d,]+)\s+reviews?\b/i);
      const rating = text.match(/\b([0-5](?:\.\d)?)\b[^\n]{0,50}\breviews?\b/i);
      const image = main.querySelector<HTMLImageElement>("img[src]");
      output.push({
        type: "host",
        id: decodeURIComponent(id),
        url: pageUrl,
        display_name: cleanText(main.querySelector("h1,h2")?.textContent, 300),
        avatar_url: absoluteHttps(image?.currentSrc || image?.src, pageUrl),
        rating: rating ? Number(rating[1]) : null,
        reviews_count: reviews ? Number(reviews[1].replace(/,/g, "")) : null,
        host_since: null,
        metadata: { page_path: parsed.pathname },
      });
    }
  }
  return output.slice(0, 30);
}

export function extractNativePageItems(
  platform: NativeScrapePlatform,
  doc: Document,
  pageUrl: string,
): NativeScrapeItem[] {
  try {
    if (platform === "pinterest") return pinterestItems(doc, pageUrl);
    if (platform === "snapchat") return snapchatStoryItems(doc, pageUrl);
    if (platform === "airbnb") return airbnbItems(doc, pageUrl);
  } catch {
    return [];
  }
  return [];
}

export function nativePageMatchesTarget(platform: NativeScrapePlatform, pageUrl: string, targetUrl: string): boolean {
  try {
    const page = new URL(pageUrl);
    const target = new URL(targetUrl);
    if (platform === "airbnb") {
      const pageId = page.pathname.match(/\/(rooms|users\/(?:show|profile))\/([^/?#]+)/i);
      const targetId = target.pathname.match(/\/(rooms|users\/(?:show|profile))\/([^/?#]+)/i);
      return Boolean(pageId && targetId && pageId[1].toLowerCase() === targetId[1].toLowerCase() && pageId[2] === targetId[2]);
    }
    if (platform === "snapchat") {
      const identity = (pathname: string) => {
        const match = pathname.match(/^\/(?:add|profile)\/([^/]+)/i) || pathname.match(/^\/@([^/]+)/);
        return match ? `profile:${decodeURIComponent(match[1]).replace(/^@/, "").toLowerCase()}` :
          (pathname.replace(/\/+$/, "") || "/").toLowerCase();
      };
      const pageHost = page.hostname.toLowerCase().replace(/^www\./, "");
      const targetHost = target.hostname.toLowerCase().replace(/^www\./, "");
      return pageHost === targetHost && identity(page.pathname) === identity(target.pathname);
    }
    if (platform === "pinterest") {
      const pageHost = page.hostname.toLowerCase().replace(/^www\./, "");
      const targetHost = target.hostname.toLowerCase().replace(/^www\./, "");
      const targetPath = target.pathname.replace(/\/+$/, "") || "/";
      const pagePath = page.pathname.replace(/\/+$/, "") || "/";
      const profileRedirect = pagePath.match(/^(\/[^/]+)\/(?:_created|_saved)$/i);
      return pageHost === targetHost && (pagePath === targetPath || profileRedirect?.[1] === targetPath);
    }
    const pageHost = page.hostname.toLowerCase().replace(/^www\./, "");
    const targetHost = target.hostname.toLowerCase().replace(/^www\./, "");
    const cleanPath = (pathname: string) => pathname.replace(/\/+$/, "") || "/";
    return pageHost === targetHost && cleanPath(page.pathname) === cleanPath(target.pathname);
  } catch {
    return false;
  }
}

export function nativePageUsername(platform: NativeScrapePlatform, pageUrl: string): string | null {
  try {
    const parsed = new URL(pageUrl);
    if (platform === "pinterest") return pinterestUsername(pageUrl);
    if (platform === "snapchat") return snapchatUsername(pageUrl);
    if (platform === "airbnb") {
      return parsed.pathname.match(/\/users\/(?:show|profile)\/([^/?#]+)/i)?.[1] || null;
    }
  } catch {
    return null;
  }
  return null;
}
