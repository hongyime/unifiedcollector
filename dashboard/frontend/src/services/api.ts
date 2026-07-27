import { API_BASE } from "../utils/constants";
import type {
  HealthStatus,
  CollectorStatus,
  CollectorDetail,
  SourceCollectionMatrix,
  MediaItem,
  MediaStats,
  HourlyIngestionRow,
  RateLimitSummary,
  MediaBrowseResult,
  StoriesOverview,
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
  StravaRouteCaptureQueue,
  CollectorsLive,
  NetworkStat,
  SocialUser,
  AccountsOverview,
  FollowEdgeStat,
  PlatformSummary,
  MessagingCoverageRow,
  IgDmThread,
  IgDmMessage,
  TtDmThread,
  TtDmMessage,
  TtProfile,
  TtProfileDetail,
  ThreadsProfile,
  ThreadsProfileDetail,
  YoutubeChannel,
  YoutubeChannelDetail,
  GithubRepo,
  GithubRepoDetail,
  GithubProfile,
  GithubProfileDetail,
  Lemon8Profile,
  Lemon8ProfileDetail,
  BeeperChat,
  BeeperChatDetail,
  DmTelemetry,
  WaSessionsResponse,
  WaChat,
  WaChatDetail,
  TelegramChat,
  TelegramMessage,
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
  health: () => get<HealthStatus>("/health?include_sources=true"),
  collectors: () => get<CollectorStatus[]>("/collectors"),
  collectorsLive: () => get<CollectorsLive>("/collectors/live"),
  sourceMatrix: () => get<SourceCollectionMatrix>("/collectors/source-matrix"),
  collectorDetail: (source: string) => get<CollectorDetail>(`/collectors/${source}`),

  media: (source?: string, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (source) params.set("source", source);
    return get<MediaItem[]>(`/media?${params}`);
  },
  mediaStats: () => get<MediaStats[]>("/media/stats"),
  hourlyIngestion: (hours = 12) => get<HourlyIngestionRow[]>(`/ingestion/hourly?hours=${hours}`),
  rateLimits: (hours = 24, limit = 100) =>
    get<RateLimitSummary>(`/rate-limits/recent?hours=${hours}&limit=${limit}`),
  mediaBrowse: (opts: { source?: string; entity?: string; type?: string; kind?: string; page?: number; pageSize?: number }) => {
    const params = new URLSearchParams();
    if (opts.source) params.set("source", opts.source);
    if (opts.entity) params.set("entity", opts.entity);
    if (opts.type) params.set("content_type", opts.type);
    if (opts.kind) params.set("kind", opts.kind);
    params.set("page", String(opts.page ?? 1));
    params.set("page_size", String(opts.pageSize ?? 24));
    return get<MediaBrowseResult>(`/media/browse?${params}`);
  },
  storiesOverview: (limit = 300) =>
    get<StoriesOverview>(`/stories/overview?limit=${limit}`),
  thumbnailUrl: (id: string) => `${API_BASE}/media/${id}/thumbnail`,
  fileUrl: (id: string) => `${API_BASE}/media/${id}/file`,

  dlq: (source?: string, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (source) params.set("source", source);
    return get<DLQItem[]>(`/dlq?${params}`);
  },

  socialNetwork: () => get<NetworkStat[]>("/social/network"),
  accounts: () => get<AccountsOverview>("/accounts"),
  followEdgesStats: () => get<FollowEdgeStat[]>("/social/follow-edges/stats"),
  platformSummary: (name: string) => get<PlatformSummary>(`/platform/${name}/summary`),
  messagingCoverage: () => get<MessagingCoverageRow[]>("/messaging/coverage"),
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

  graph: (source = "github", limit = 5000) => get<GraphData>(`/graph?source=${source}&limit=${limit}`),

  waUsers: (search?: string, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (search) params.set("search", search);
    return get<WhatsAppUser[]>(`/whatsapp/users?${params}`);
  },
  waUserHistory: (jid: string) => get<UserHistoryEntry[]>(`/whatsapp/users/${jid}/history`),

  // WhatsApp chats + messages (recent-first list, chronological messages).
  // jid is the platform_chat_id (may contain @ and dots — encodeURIComponent
  // handles that; slashes are OK because the endpoint uses :path).
  waChats: (limit = 100) => get<WaChat[]>(`/whatsapp/chats?limit=${limit}`),
  waChat: (jid: string, limit = 200) =>
    get<WaChatDetail>(
      `/whatsapp/chat/${encodeURIComponent(jid)}?limit=${limit}`,
    ),

  igDmThreads: (owner?: string, limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (owner) params.set("owner", owner);
    return get<IgDmThread[]>(`/instagram/dms/threads?${params}`);
  },
  igDmThread: (threadId: string) =>
    get<{ thread: IgDmThread | null; messages: IgDmMessage[] }>(
      `/instagram/dms/thread/${encodeURIComponent(threadId)}`,
    ),

  // TikTok DMs — same schema shape as IG but backed by tiktok_dm{,_thread}
  // populated by the extension's client-side protobuf decoder.
  ttDmThreads: (owner?: string, limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (owner) params.set("owner", owner);
    return get<TtDmThread[]>(`/tiktok/dms/threads?${params}`);
  },
  ttDmThread: (threadId: string) =>
    get<{ thread: TtDmThread | null; messages: TtDmMessage[] }>(
      `/tiktok/dms/thread/${encodeURIComponent(threadId)}`,
    ),

  // TikTok public feed (profiles + posts). List sorts by follower count DESC;
  // detail returns the profile row + newest ~200 posts with media UUIDs
  // pre-joined for /media/<uuid>/thumbnail rendering.
  tiktokProfiles: (limit = 100) =>
    get<TtProfile[]>(`/tiktok/profiles?limit=${limit}`),
  tiktokProfile: (username: string, limit = 200) =>
    get<TtProfileDetail>(
      `/tiktok/profile/${encodeURIComponent(username)}?limit=${limit}`,
    ),

  threadsProfiles: (limit = 100) =>
    get<ThreadsProfile[]>(`/threads/profiles?limit=${limit}`),
  threadsProfile: (username: string, limit = 200) =>
    get<ThreadsProfileDetail>(
      `/threads/profile/${encodeURIComponent(username)}?limit=${limit}`,
    ),

  youtubeChannels: (limit = 100) =>
    get<YoutubeChannel[]>(`/youtube/channels?limit=${limit}`),
  youtubeChannel: (channelId: string, limit = 200) =>
    get<YoutubeChannelDetail>(
      `/youtube/channel/${encodeURIComponent(channelId)}?limit=${limit}`,
    ),

  githubProfiles: (limit = 100) =>
    get<GithubProfile[]>(`/github/profiles?limit=${limit}`),
  githubProfile: (owner: string, limit = 200) =>
    get<GithubProfileDetail>(
      `/github/profile/${encodeURIComponent(owner)}?limit=${limit}`,
    ),
  githubRepos: (limit = 100) =>
    get<GithubRepo[]>(`/github/repos?limit=${limit}`),
  githubRepo: (fullName: string, limit = 200) =>
    get<GithubRepoDetail>(
      `/github/repo/${encodeURIComponent(fullName)}?limit=${limit}`,
    ),

  lemon8Profiles: (limit = 100) =>
    get<Lemon8Profile[]>(`/lemon8/profiles?limit=${limit}`),
  lemon8Profile: (username: string, limit = 200) =>
    get<Lemon8ProfileDetail>(
      `/lemon8/profile/${encodeURIComponent(username)}?limit=${limit}`,
    ),

  beeperChats: (limit = 100, network?: string) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (network) params.set("network", network);
    return get<BeeperChat[]>(`/beeper/chats?${params}`);
  },
  beeperChat: (chatId: string, limit = 200) =>
    get<BeeperChatDetail>(
      `/beeper/chat/${encodeURIComponent(chatId)}?limit=${limit}`,
    ),

  // P1.2: passive telemetry surface. Populated by dm_probe_handler +
  // dm_sample_handler in src/bridges/ig_ingest.py. Used by the counter panel
  // at the top of InstagramDmPage.
  dmTelemetry: () => get<DmTelemetry>("/dm/telemetry"),

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
    get<{ bridge: string; status: string; qr: string; ready: boolean; error: string | null; qr_available?: boolean; last_qr_at?: string | null }>(
      `/whatsapp/qr/${bridge}`
    ),
  waDisconnect: (bridge: 1 | 2) => post<{ bridge: string; ok: boolean; status?: string; error?: string }>(`/whatsapp/${bridge}/disconnect`, {}),
  waReconnect: (bridge: 1 | 2) => post<{ bridge: string; ok: boolean; status?: string; error?: string }>(`/whatsapp/${bridge}/reconnect`, {}),
  waFreshQr: (bridge: 1 | 2) => post<{ bridge: string; ok: boolean; status?: string; warning?: string; error?: string }>(`/whatsapp/${bridge}/fresh-qr`, {}),

  // Per-bridge session identity (phone number, push name, connected state).
  // Populated after a QR scan so the Link page can show WHICH account is on
  // WHICH bridge slot. Sourced from the bridge's /session endpoint.
  waSessions: () => get<WaSessionsResponse>("/whatsapp/sessions"),

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

  // Rich per-chat detail view (dashboard /telegram/chats). List → detail is
  // keyed on telegram_chats.platform_chat_id (e.g. "-1001234567890"), the
  // same human-visible id that shows up in collector logs.
  telegramChats: (owner?: string, limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (owner) params.set("owner", owner);
    return get<TelegramChat[]>(`/telegram/chats?${params}`);
  },
  telegramChat: (chatId: string, limit = 200) =>
    get<{ chat: TelegramChat | null; messages: TelegramMessage[] }>(
      `/telegram/chat/${encodeURIComponent(chatId)}?limit=${limit}`,
    ),



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
  stravaRouteCaptureQueue: (limit = 8) =>
    get<StravaRouteCaptureQueue>(`/strava/route-capture-queue?limit=${limit}`),
};
