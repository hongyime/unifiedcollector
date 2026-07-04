import { API_BASE } from "../utils/constants";
import type {
  HealthStatus,
  CollectorStatus,
  CollectorDetail,
  MediaItem,
  MediaStats,
  MediaBrowseResult,
  DLQItem,
  Target,
  Schedule,
  Run,
  GraphData,
  WhatsAppUser,
  UserHistoryEntry,
  DiscoveredLink,
  LinkStats,
  AuthResponse,
  StravaAthleteSummary,
  StravaFeedDate,
  StravaFeedActivity,
  StravaFeedStats,
  CollectorsLive,
  NetworkStat,
  SocialUser,
  AccountsOverview,
  FollowEdgeStat,
} from "./types";

function getToken(): string | null {
  return localStorage.getItem("auth_token");
}

function authHeaders(): HeadersInit {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail: unknown = undefined;
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    const err = new Error(`${res.status} ${res.statusText}`) as Error & { status?: number; detail?: unknown };
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.json() as Promise<T>;
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  health: () => get<HealthStatus>("/health"),
  collectors: () => get<CollectorStatus[]>("/collectors"),
  collectorsLive: () => get<CollectorsLive>("/collectors/live"),
  collectorDetail: (source: string) => get<CollectorDetail>(`/collectors/${source}`),

  media: (source?: string, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (source) params.set("source", source);
    return get<MediaItem[]>(`/media?${params}`);
  },
  mediaStats: () => get<MediaStats[]>("/media/stats"),
  mediaBrowse: (opts: { source?: string; entity?: string; type?: string; page?: number; pageSize?: number }) => {
    const params = new URLSearchParams();
    if (opts.source) params.set("source", opts.source);
    if (opts.entity) params.set("entity", opts.entity);
    if (opts.type) params.set("content_type", opts.type);
    params.set("page", String(opts.page ?? 1));
    params.set("page_size", String(opts.pageSize ?? 24));
    return get<MediaBrowseResult>(`/media/browse?${params}`);
  },
  thumbnailUrl: (id: number) => `${API_BASE}/media/${id}/thumbnail`,
  fileUrl: (id: number) => `${API_BASE}/media/${id}/file`,

  dlq: (source?: string, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (source) params.set("source", source);
    return get<DLQItem[]>(`/dlq?${params}`);
  },

  socialNetwork: () => get<NetworkStat[]>("/social/network"),
  accounts: () => get<AccountsOverview>("/accounts"),
  followEdgesStats: () => get<FollowEdgeStat[]>("/social/follow-edges/stats"),
  socialUsers: (opts: { platform?: string; q?: string; limit?: number } = {}) => {
    const params = new URLSearchParams({ limit: String(opts.limit ?? 60) });
    if (opts.platform) params.set("platform", opts.platform);
    if (opts.q) params.set("q", opts.q);
    return get<SocialUser[]>(`/social/users?${params}`);
  },
  targets: (source?: string) => {
    const params = source ? `?source=${source}` : "";
    return get<Target[]>(`/targets${params}`);
  },
  createTarget: (source: string, target: string, priority = 0, force = false) =>
    post(`/targets${force ? "?force=true" : ""}`, { source, target, priority }),
  deleteTarget: (id: number) => del(`/targets/${id}`),

  schedules: () => get<Schedule[]>("/schedules"),
  updateSchedule: (source: string, interval_hours: number, enabled: boolean) =>
    put(`/schedules/${source}`, { source, interval_hours, enabled }),

  runs: (source?: string, limit = 20) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (source) params.set("source", source);
    return get<Run[]>(`/runs?${params}`);
  },
  runDetail: (id: number) => get<Run>(`/runs/${id}`),

  graph: (source = "github") => get<GraphData>(`/graph?source=${source}`),

  waUsers: (search?: string, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (search) params.set("search", search);
    return get<WhatsAppUser[]>(`/whatsapp/users?${params}`);
  },
  waUserHistory: (jid: string) => get<UserHistoryEntry[]>(`/whatsapp/users/${jid}/history`),

  waLinks: (opts?: { linkType?: string; status?: string; limit?: number }) => {
    const params = new URLSearchParams();
    if (opts?.linkType) params.set("link_type", opts.linkType);
    if (opts?.status) params.set("status", opts.status);
    params.set("limit", String(opts?.limit ?? 100));
    return get<DiscoveredLink[]>(`/whatsapp/links?${params}`);
  },
  waLinkStats: () => get<LinkStats[]>("/whatsapp/links/stats"),

  // WhatsApp bridge QR linking (auto-refresh on the client)
  waQr: (bridge: 1 | 2) =>
    get<{ bridge: string; status: string; qr: string; ready: boolean; error: string | null }>(
      `/whatsapp/qr/${bridge}`
    ),
  waDisconnect: (bridge: 1 | 2) => post<{ bridge: string; ok: boolean; status?: string; error?: string }>(`/whatsapp/${bridge}/disconnect`, {}),
  waReconnect: (bridge: 1 | 2) => post<{ bridge: string; ok: boolean; status?: string; error?: string }>(`/whatsapp/${bridge}/reconnect`, {}),

  // Telegram collection stats
  telegramStats: () =>
    get<{
      totals: Record<string, number>;
      recent: Record<string, number>;
      top_chats: { title: string | null; username: string | null; messages: number }[];
    }>("/api/telegram/stats"),

  // Telegram accounts
  telegramAccounts: () =>
    get<
      {
        name: string;
        phone: string | null;
        status: string;
        owner_bot: string | null;
        created_at: string | null;
        last_connected_at: string | null;
        last_error: string | null;
      }[]
    >("/api/telegram/accounts"),
  telegramAccountEnable: (name: string) =>
    post<{ status: string }>(`/api/telegram/accounts/${name}/enable`, {}),
  telegramAccountDisable: (name: string) =>
    post<{ status: string }>(`/api/telegram/accounts/${name}/disable`, {}),



  login: (username: string, password: string) =>
    post<AuthResponse>("/auth/login", { username, password }),
  me: () => get<{ username: string; role: string }>("/auth/me"),

  // Strava following-feed playback
  stravaAthletes: (limit = 200) =>
    get<StravaAthleteSummary[]>(`/strava/athletes?limit=${limit}`),
  stravaFeedDates: (athleteId?: number, from?: string, to?: string) => {
    const params = new URLSearchParams();
    if (athleteId !== undefined) params.set("athlete_id", String(athleteId));
    if (from) params.set("from", from);
    if (to) params.set("to", to);
    return get<StravaFeedDate[]>(`/strava/feed/dates?${params}`);
  },
  stravaFeedActivities: (date: string, athleteId?: number, limit = 200, offset = 0) => {
    const params = new URLSearchParams({ date, limit: String(limit), offset: String(offset) });
    if (athleteId !== undefined) params.set("athlete_id", String(athleteId));
    return get<StravaFeedActivity[]>(`/strava/feed/activities?${params}`);
  },
  stravaFeedStats: (athleteId?: number) => {
    const params = new URLSearchParams();
    if (athleteId !== undefined) params.set("athlete_id", String(athleteId));
    return get<StravaFeedStats>(`/strava/feed/stats?${params}`);
  },
};
