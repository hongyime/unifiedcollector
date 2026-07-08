export interface HealthStatus {
  status: "ok" | "degraded";
  database: string;
  drive: string;
}

export interface CollectorStatus {
  service: string;
  last_processed_id: string | null;
  last_processed_at: string | null;
  status: string;
}

export interface MediaItem {
  id: string;
  source: string;
  entity_name: string;
  content_type: string;
  filename: string;
  file_size: number | null;
  collected_at: string;
}

export interface MediaStats {
  source: string;
  total_items: number;
  total_bytes: number;
  last_collected: string | null;
}

export interface DLQItem {
  id: number;
  source: string;
  entity_id: string | null;
  content_id: string | null;
  error_message: string | null;
  retry_count: number;
  created_at: string;
}

export interface Target {
  id: number;
  source: string;
  target: string;
  priority: number;
  created_at: string;
}

export interface Schedule {
  source: string;
  interval_hours: number;
  enabled: boolean;
  next_run: string | null;
}

export interface Run {
  id: number;
  source: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  items_collected: number;
  errors: number;
}

export interface CollectorDetail {
  source: string;
  cursor: CollectorStatus | null;
  media_count: number;
  error_count: number;
  recent_items: MediaItem[];
}

export interface GraphNode {
  id: string;
}

export interface GraphEdge {
  source_user: string;
  target_user: string;
  edge_type: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface WhatsAppUser {
  platform_user_id: string;
  name: string | null;
  pushname: string | null;
  status: string | null;
  about: string | null;
  photo_url: string | null;
  collected_at: string | null;
  updated_at: string | null;
}

export interface UserHistoryEntry {
  id: string;
  content: string | null;
  caption: string | null;
  message_type: string | null;
  collected_at: string;
}

export interface IgDmThread {
  thread_id: string;
  title: string | null;
  participants: string[] | null;
  owner_account: string | null;
  last_activity: string | null;
  message_count?: number;
  last_message_ts?: string | null;
}

export interface IgDmMessage {
  message_id: string;
  sender_id: string | null;
  sender_username: string | null;
  text: string | null;
  item_type: string | null;
  timestamp: string | null;
  is_from_me: boolean;
  owner_account: string | null;
}

// TikTok DMs — captured through the extension's WS-hook client-side decoder
// (Option B of #39). Table columns match tiktok_dm{,_thread}; API endpoints
// under /tiktok/dms/. Thread IDs are the conversation_id string
// '0:1:UID_A:UID_B' — kept as `thread_id` in the API shape for symmetry
// with the IG DM types.
export interface TtDmThread {
  thread_id: string;
  conversation_type: number | null;
  participants: string[] | null;
  owner_account: string | null;
  last_activity: string | null;
  message_count?: number;
  last_message_ts?: string | null;
}
export interface TtDmMessage {
  message_id: string;
  sender_id: string | null;
  sender_secuid: string | null;
  text: string | null;
  awe_type: number | null;
  message_type: number | null;
  timestamp: string | null;
  is_from_me: boolean;
  owner_account: string | null;
  client_message_id: string | null;
  is_stranger: boolean | null;
  media_url: string | null;
}

// Passive DM WS-hook telemetry (P1.2). One entry per platform the extension's
// observe-only WS wrapper has ever seen; `probe`/`sample` mirror event_type
// in dm_probe_log. Null last_seen means "never" — most useful signal for
// Instagram, which stays at 1 placeholder sample until real DM traffic
// (edge-chat.instagram.com/chat frames ≥ 24B) actually shows up.
export interface DmTelemetryBucket {
  all_time: number;
  last_24h: number;
  last_1h: number;
  last_seen: string | null;
  max_frame_size: number | null;
  min_frame_size: number | null;
}

// WhatsApp bridge session identity (bridge /session endpoint + dashboard
// /whatsapp/sessions aggregator). Returned per bridge slot after the user
// scans a QR — phone_number + push_name identifies which account got
// linked to which slot.
export interface WaBridgeSession {
  bridge: string;               // "1" | "2"
  ok: boolean;                  // false when the bridge itself is unreachable
  error?: string;
  connected?: boolean;          // bridge paired AND socket open
  session_name?: string | null; // bridge slot label from env (e.g. "session_1")
  wid?: string | null;          // full JID e.g. 6591234567:12@s.whatsapp.net
  phone_number?: string | null; // just the digits
  push_name?: string | null;    // WhatsApp display name, if set
}
export interface WaSessionsResponse {
  sessions: WaBridgeSession[];
}
export interface DmTelemetryPlatform {
  platform: string;
  probe: DmTelemetryBucket;
  sample: DmTelemetryBucket;
  // P1.3: browser-side WS-hook liveness. Null when no heartbeat has ever
  // arrived — the watchdog treats that as "not installed" (won't alert).
  hook: DmHookHeartbeat | null;
}
export interface DmHookHeartbeat {
  last_seen: string | null;
  probes_sent: number;
  samples_shipped: number;
  extension_version: string | null;
  owner_count: number;
}
export interface DmTelemetry {
  platforms: DmTelemetryPlatform[];
  generated_at: string;
}

export interface DiscoveredLink {
  id: number;
  link: string;
  link_type: string;
  source_jid: string | null;
  status: string;
  discovered_at: string;
}

export interface LinkStats {
  link_type: string;
  status: string;
  count: number;
}

export interface AuthUser {
  username: string;
  role: "viewer" | "operator" | "admin";
}

export interface AuthResponse {
  token: string;
  username: string;
  role: string;
}

export interface MediaBrowseResult {
  total: number;
  page: number;
  page_size: number;
  items: MediaItem[];
}

export interface StravaAthleteSummary {
  platform_athlete_id: number;
  username: string | null;
  firstname: string | null;
  lastname: string | null;
  profile: string | null;
  activity_count: number;
}

export interface StravaFeedDate {
  date: string;
  count: number;
}

export interface StravaFeedActivity {
  platform_activity_id: number;
  name: string | null;
  type: string | null;
  sport_type: string | null;
  distance: number | null;
  moving_time: number | null;
  elapsed_time: number | null;
  total_elevation_gain: number | null;
  start_date: string | null;
  platform_athlete_id: number | null;
  username: string | null;
  firstname: string | null;
  lastname: string | null;
  profile: string | null;
}

export interface StravaFeedStats {
  total_activities: number;
  total_distance: number;
  total_moving_time: number;
  total_elevation_gain: number;
  earliest: string | null;
  latest: string | null;
}

export interface CollectorLiveSource {
  source: string;
  status: "live" | "stale" | "degraded" | "dead" | "unknown";
  age_seconds: number | null;
}

export interface CollectorsLive {
  total: number;
  live: number;
  sources: CollectorLiveSource[];
}

export interface NetworkStat {
  platform: string;
  total: number;
  following: number;
  followers: number;
}

export interface SocialUser {
  platform: string;
  uid: string;
  username: string | null;
  display_name: string | null;
  profile_photo_url: string | null;
  times_seen: number;
  contexts: string[];
  last_seen: string | null;
}

export interface TelegramAccountRow {
  name: string;
  phone: string | null;
  status: string | null;
  last_connected_at: string | null;
  last_error: string | null;
}
export interface WhatsAppSession {
  session: string;
  ready: boolean;
  status: string;
  error?: string;
}
export interface CookieAccount {
  source: string;
  account: string;
  file: string;
  age_days: number | null;
  expiry_days: number | null;
  has_session: boolean;
  live_status: "ok" | "dead" | "unknown" | null;
  needs_refresh: boolean;
  reason: string | null;
  health: string;
}
export interface AccountsOverview {
  telegram: TelegramAccountRow[];
  whatsapp: WhatsAppSession[];
  cookies: CookieAccount[];
  health: Record<string, string>;
}

export interface FollowEdgeStat {
  platform: string;
  owner_account: string;
  followers: number;
  following: number;
  last_seen: string | null;
}

export interface PlatformSummary {
  platform: string;
  media_count: number;
  media_last: string | null;
  media_recent: MediaItem[];
  users_count: number;
  posts_count?: number;
  posts_label?: string;
  messages_count?: number;
  messages_last?: string | null;
  follow_edges: { owner_account: string; followers: number; following: number }[];
  live?: string | null;
  age_seconds?: number | null;
}
