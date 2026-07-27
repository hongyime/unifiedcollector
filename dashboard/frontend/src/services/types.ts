export interface HealthStatus {
  status: "ok" | "degraded";
  database: string;
  drive: string;
  vault?: {
    root: string;
    available: boolean;
    writable: boolean;
    free_bytes: number | null;
    total_bytes: number | null;
    error?: string | null;
    sidecar_failures: number;
    artifacts_queued: number;
    artifacts_partial: number;
    counts_error?: string | null;
  };
  backups?: {
    status: "ok" | "refreshing" | "stale" | "missing" | "error";
    root: string;
    latest_path: string | null;
    latest_created_at: string | null;
    latest_age_seconds: number | null;
    latest_size_bytes: number | null;
    backup_count: number;
    in_progress: boolean;
    in_progress_count?: number;
    stale_in_progress_count?: number;
    stale_in_progress_oldest_age_seconds?: number | null;
    max_age_hours?: number | null;
    error?: string | null;
  };
  sources?: CollectorLiveSource[];
  source_issues?: CollectorLiveSource[];
  whatsapp_bridge_health?: Record<string, unknown> | null;
  browser_extension?: BrowserExtensionHealth | null;
}

export interface BrowserExtensionHook {
  platform: string;
  last_seen_at: string | null;
  age_seconds: number;
  extension_version: string | null;
  version_ok: boolean;
  owner_count: number;
  probes_sent: number;
  samples_shipped: number;
}

export interface BrowserExtensionIngest {
  platform: string;
  endpoint: string;
  requests: number;
  observed_count: number;
  stored_count: number;
  last_seen_at: string | null;
  age_seconds: number;
  extension_version: string | null;
  version_ok: boolean;
}

export interface BrowserExtensionIssue {
  platform: string;
  endpoint?: string | null;
  kind: "hook_stale" | "extension_version_mismatch" | string;
  detail: string;
  age_seconds?: number;
  extension_version?: string | null;
  expected_version?: string | null;
}

export interface BrowserExtensionHealth {
  expected_version: string | null;
  hooks: BrowserExtensionHook[];
  ingest: BrowserExtensionIngest[];
  issues: BrowserExtensionIssue[];
}

export interface CollectorStatus {
  service: string;
  last_processed_id: string | null;
  last_processed_at: string | null;
  status: string;
}

export interface SourceWindowCounts {
  records: number;
  messages: number;
  media_items: number;
  rate_limits: number;
  access_errors: number;
  latest_record_at: string | null;
  latest_media_at: string | null;
  latest_event_at: string | null;
}

export interface SourceBlocker {
  kind: string;
  severity: "ok" | "warning" | "error" | string;
  summary: string;
  next_action: string;
}

export interface SourceRateLimitState {
  active_now: boolean;
  active_until: string | null;
  streak: number | null;
  latest_status_code: number | null;
  latest_account: string | null;
  latest_scope: string | null;
  latest_reason: string | null;
}

export interface SourceCollectionMatrixRow {
  source: string;
  status: CollectorLiveSource["status"];
  collection_mode: string | null;
  collection_methods: string[];
  freshness_basis: string | null;
  age_seconds: number | null;
  stale_after_seconds: number | null;
  detail: string | null;
  source_health_status?: string | null;
  source_health_error?: string | null;
  bridge_status?: string | null;
  bridge_detail?: string | null;
  current_hour: SourceWindowCounts;
  last_24h: SourceWindowCounts;
  total_media_items: number;
  total_media_bytes: number;
  latest_media_at: string | null;
  rate_limit: SourceRateLimitState;
  extension_issues: BrowserExtensionIssue[];
  blocker: SourceBlocker;
}

export interface SourceCollectionMatrix {
  generated_at: string;
  current_hour_started_at: string;
  sources: SourceCollectionMatrixRow[];
  whatsapp_bridge_health?: Record<string, unknown> | null;
  browser_extension?: {
    expected_version: string | null;
    issues: BrowserExtensionIssue[];
  } | null;
  errors?: Array<{ section: string; error: string }>;
}

export interface RunIngestionCounts {
  records: number;
  messages: number;
  media_items: number;
  rate_limits: number;
  access_errors: number;
  latest_at: string | null;
  window_seconds: number | null;
  basis?: string;
  exact_window?: boolean;
}

export interface MediaItem {
  id: string;
  source: string;
  entity_name: string;
  content_type: string;
  kind?: string | null;
  filename: string;
  file_size: number | null;
  collected_at: string;
}

export interface StoryAccount {
  source: string;
  entity_name: string;
  story_count: number;
  highlight_count: number;
  total: number;
  newest: string | null;
}

export interface StoriesOverview {
  stats: {
    stories?: number;
    highlights?: number;
    accounts?: number;
    sources?: number;
    newest?: string | null;
  };
  accounts: StoryAccount[];
}

export interface MediaStats {
  source: string;
  total_items: number;
  total_bytes: number;
  last_collected: string | null;
  last_activity?: string | null;
  activity_basis?: string | null;
  live?: CollectorLiveSource["status"];
  age_seconds?: number | null;
  stale_after_seconds?: number | null;
  collection_mode?: string | null;
  freshness_basis?: string | null;
  health_detail?: string | null;
  source_health_status?: string | null;
  source_health_error?: string | null;
}

export interface HourlyIngestionRow {
  source: string;
  hour: string;
  records: number;
  record_label: string;
  media_items: number;
  messages: number;
  rate_limits: number;
  access_errors: number;
}

export interface RateLimitEvent {
  id: number;
  source: string;
  account: string | null;
  scope: string | null;
  status_code: number | null;
  cooldown_seconds: number | null;
  reason: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ActiveRateLimit {
  service: string;
  last_processed_id: string | null;
  last_processed_at: string | null;
  status: string | null;
  active_until: string | null;
  streak: number | null;
  active_now: boolean;
}

export interface RateLimitRecentSummary {
  source: string;
  account: string | null;
  scope: string | null;
  status_code: number | null;
  count: number;
  cooldown_seconds: number | null;
  reason: string | null;
  first_seen_at: string;
  last_seen_at: string;
  active_until: string | null;
  active_now: boolean;
}

export interface RateLimitSummary {
  events: RateLimitEvent[];
  active: ActiveRateLimit[];
  recent_summary: RateLimitRecentSummary[];
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
  target_id?: string;
  target_name?: string | null;
  target_type?: string | null;
  status?: string | null;
  target?: string;
  priority: number;
  metadata?: Record<string, unknown> | null;
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
  items_label?: string;
  ingestion?: RunIngestionCounts;
  ingestion_items?: number;
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

// TikTok public feed — /tiktok/profiles + /tiktok/profile/{username}. Backs
// the rich feed page (left pane = profile picker sorted by follower count,
// right pane = post grid with thumbnails joined from media_items).
export interface TtProfile {
  platform_user_id: string;
  username: string | null;
  nickname: string | null;
  avatar_url: string | null;
  bio: string | null;
  followers_count: number | null;
  following_count: number | null;
  heart_count: number | null;
  video_count: number | null;
  digg_count?: number | null;
  is_verified: boolean;
  is_private?: boolean;
  updated_at: string | null;
  collected_at: string | null;
  // list-view extras (list endpoint only)
  last_post_at?: string | null;
  posts_collected?: number;
}

export interface TtPost {
  platform_post_id: string;
  title: string | null;
  description: string | null;
  video_url: string | null;
  cover_image_url: string | null;
  hashtags: string[] | null;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  duration: number | null;
  music_title: string | null;
  music_author: string | null;
  create_time: string | null;
  collected_at: string | null;
  // media_items UUID (null when the video/photo file was never downloaded
  // — e.g. metadata-only backfill). Content_type disambiguates video vs.
  // photo carousel for the card layout.
  media_item_id: string | null;
  media_content_type: string | null;
  // Server-built canonical URL — prefers media_items.source_url (the
  // collector's authoritative build) and falls back to the standard
  // https://www.tiktok.com/@user/video/{id} pattern.
  post_url: string;
}

export interface TtProfileDetail {
  profile: TtProfile | null;
  posts: TtPost[];
}

export interface ThreadsProfile {
  username: string;
  posts_collected: number;
  last_post_at: string | null;
  avatar_url: string | null;
}

export interface ThreadsPost {
  platform_post_id: string;
  caption: string | null;
  hashtags: string[] | null;
  likes_count: number | null;
  comments_count: number | null;
  reposts_count: number | null;
  media_type: string | null;
  platform_created_at: string | null;
  collected_at: string | null;
  media_item_id: string | null;
  media_content_type: string | null;
  post_url: string;
}

export interface ThreadsProfileDetail {
  profile: ThreadsProfile | null;
  posts: ThreadsPost[];
}

export interface YoutubeChannel {
  platform_channel_id: string;
  title: string | null;
  custom_url: string | null;
  thumbnail_url: string | null;
  description: string | null;
  subscriber_count: number | null;
  video_count: number | null;
  view_count: number | null;
  updated_at: string | null;
  videos_collected?: number;
  last_video_at?: string | null;
}

export interface YoutubeVideo {
  platform_video_id: string;
  title: string | null;
  description: string | null;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  duration: string | null;
  platform_published_at: string | null;
  collected_at: string | null;
  media_item_id: string | null;
  media_content_type: string | null;
  video_url: string;
}

export interface YoutubeChannelDetail {
  channel: YoutubeChannel | null;
  videos: YoutubeVideo[];
}

export interface GithubRepo {
  platform_repo_id: number;
  name: string | null;
  full_name: string;
  description: string | null;
  language: string | null;
  stargazers_count: number | null;
  forks_count: number | null;
  open_issues_count: number | null;
  platform_updated_at: string | null;
  commits_collected?: number;
  last_commit_at?: string | null;
}

export interface GithubCommit {
  sha: string;
  author_name: string | null;
  author_login: string | null;
  message: string | null;
  date: string | null;
  files_changed: number | null;
  insertions: number | null;
  deletions: number | null;
  collected_at: string | null;
  commit_url: string;
  repo_full_name?: string | null;
}

export interface GithubRepoDetail {
  repo: GithubRepo | null;
  commits: GithubCommit[];
}

export interface GithubProfile {
  owner: string;
  repos_collected: number;
  stargazers_count: number | null;
  forks_count: number | null;
  updated_at: string | null;
  collected_at: string | null;
  last_commit_at?: string | null;
  commits_loaded?: number;
}

export interface GithubProfileDetail {
  profile: GithubProfile | null;
  repos: GithubRepo[];
  commits: GithubCommit[];
}

export interface Lemon8Profile {
  platform_user_id: string;
  username: string | null;
  nickname: string | null;
  avatar_url: string | null;
  bio: string | null;
  followers_count: number | null;
  following_count: number | null;
  like_count: number | null;
  updated_at: string | null;
  posts_collected?: number;
  last_post_at?: string | null;
}

export interface Lemon8Post {
  platform_post_id: string;
  title: string | null;
  description: string | null;
  music_title: string | null;
  like_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  platform_created_at: string | null;
  collected_at: string | null;
  media_item_id: string | null;
  media_content_type: string | null;
  post_url: string;
}

export interface Lemon8ProfileDetail {
  profile: Lemon8Profile | null;
  posts: Lemon8Post[];
}

export interface BeeperChat {
  chat_id: string;
  local_chat_id: string | null;
  network: string;
  title: string | null;
  img_url: string | null;
  chat_type: string | null;
  is_direct: boolean | null;
  account_id: string | null;
  last_seen_at: string | null;
  messages_collected?: number;
  last_message_at?: string | null;
}

export interface BeeperMessage {
  message_id: string;
  network: string;
  sender_id: string | null;
  sender_name: string | null;
  text: string | null;
  timestamp: string | null;
  sort_key: string | null;
  is_media: boolean | null;
  media_url: string | null;
  media_type: string | null;
  is_deleted: boolean | null;
  deleted_at: string | null;
  ingested_at: string | null;
  media_item_id: string | null;
}

export interface BeeperChatDetail {
  chat: BeeperChat | null;
  messages: BeeperMessage[];
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
  status?: string | null;       // ready, awaiting_scan, connecting, etc.
  registered?: boolean;         // Baileys has valid local creds
  qr_available?: boolean;       // bridge currently has a scannable QR payload
  last_qr_at?: string | null;   // ISO timestamp of newest QR
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

// WhatsApp chats + messages — /whatsapp/chats and /whatsapp/chat/{jid}. The
// platform_chat_id (JID) doubles as the API path parameter; message rows carry
// pushname/phone_number joined from whatsapp_users and a media_id joined from
// media_items on file_path = media_url so the frontend can reuse
// /media/{id}/thumbnail without a new proxy.
export interface WaChat {
  platform_chat_id: string;
  name: string | null;
  is_group: boolean;
  chat_type: "dm" | "group" | "channel" | "broadcast" | string;
  participant_count: number | null;
  description?: string | null;
  updated_at: string | null;
  // No message_count in the list view — a per-chat count(*) LATERAL on the
  // 46k-row messages table is ~500ms of unnecessary latency for a datapoint
  // the sidebar doesn't need. Loaded messages in the detail view convey the
  // same "how much" signal implicitly.
  last_message_ts?: string | null;
  last_text?: string | null;
  last_from_me?: boolean | null;
  last_media_mime?: string | null;
}

export interface WaMessage {
  platform_message_id: string;
  from_me: boolean;
  text: string | null;
  media_url: string | null;
  media_mime_type: string | null;
  media_size: number | null;
  thumbnail_url: string | null;
  timestamp: string | null;
  is_deleted: boolean;
  deleted_at: string | null;
  quoted_text: string | null;
  forward_from_name: string | null;
  sender_jid: string | null;
  sender_pushname: string | null;
  sender_name: string | null;
  sender_phone: string | null;
  media_id: string | null;
  // Server truncates individual messages > 4 KB so the response body doesn't
  // blow past the 3s SLA on chats with rare oversized forwards. Untouched
  // when short.
  text_truncated?: boolean;
  text_full_length?: number;
}

export interface WaChatDetail {
  chat: WaChat | null;
  messages: WaMessage[];
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
  total_estimated?: boolean;
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
  distance_unit: string | null;
  moving_time: number | null;
  elapsed_time: number | null;
  total_elevation_gain: number | null;
  average_speed: number | null;
  start_date: string | null;
  summary_polyline: string | null;
  start_latlng: string | null;
  stream_status: string | null;
  route_status: string | null;
  route_status_detail: string | null;
  gps_rate_limit_at: string | null;
  gps_rate_limit_until: string | null;
  gps_rate_limit_reason: string | null;
  gps_rate_limit_context: string | null;
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
  route_coverage?: {
    total: number;
    mapped: number;
    queued: number;
    start_only: number;
    privacy_zone: number;
    no_gps: number;
    unverifiable: number;
    browser_captured: number;
    completion_pct: number;
    recent_gps_429_events: number;
    active_gps_cooldown_until: string | null;
    active_gps_cooldown_reason: string | null;
    latest_browser_capture_at: string | null;
  };
}

export interface StravaRouteCaptureQueueItem {
  platform_activity_id: number;
  activity_url: string;
  name: string | null;
  type: string | null;
  sport_type: string | null;
  start_date: string | null;
  start_latlng: string | null;
  stream_status: string | null;
  platform_athlete_id: number | null;
  athlete_name: string | null;
  proximity_tier: number;
  target_priority: number;
  last_browser_visit_at: string | null;
}

export interface StravaRouteCaptureQueue {
  items: StravaRouteCaptureQueueItem[];
  cooldown: {
    active: boolean;
    until: string | null;
    reason: string | null;
  };
  recent_visit_ttl_hours: number;
}

export interface CollectorLiveSource {
  source: string;
  status: "live" | "stale" | "degraded" | "dead" | "unknown" | "unpaired" | "unreachable";
  age_seconds: number | null;
  stale_after_seconds?: number | null;
  collection_mode?: string | null;
  freshness_basis?: string | null;
  source_health_status?: string | null;
  source_health_error?: string | null;
  detail?: string | null;
  bridge_status?: string | null;
  bridge_detail?: string | null;
  whatsapp_bridges?: WaBridgeSession[];
}

export interface CollectorsLive {
  total: number;
  live: number;
  degraded?: number;
  sources: CollectorLiveSource[];
  whatsapp_bridge_health?: Record<string, unknown> | null;
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

// Telegram: rich chats+messages detail page (dashboard /telegram/chats). Keyed
// on `platform_chat_id` (the human-visible varchar UNIQUE), not the internal
// UUID, so URLs stay stable across DB rebuilds and match collector log ids.
export interface TelegramChat {
  platform_chat_id: string;
  title: string | null;
  username: string | null;
  type: string | null;
  description: string | null;
  members_count: number | null;
  updated_at: string | null;
  collected_at: string | null;
  message_count?: number;  // only set on the detail endpoint
}

// One row from /telegram/chat/{chat_id}.messages. `is_deleted` is derived from
// telegram_messages.metadata->>'deleted' (the collector's partial index),
// and `media_item_id` — when present — is the UUID for the existing
// /media/<uuid>/thumbnail + /media/<uuid>/file endpoints, so the client can
// show inline media without an extra fetch.
export interface TelegramMessage {
  platform_message_id: string;
  text: string | null;
  caption: string | null;
  media_type: string | null;
  media_file_id: string | null;
  is_edited: boolean;
  edit_date: string | null;
  reply_to_message_id: string | null;
  platform_created_at: string | null;
  collected_at: string;
  is_deleted: boolean;
  deleted_at: string | null;
  sender_platform_id: string | null;
  sender_username: string | null;
  sender_first_name: string | null;
  sender_last_name: string | null;
  media_item_id: string | null;
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
  source_mode?: string | null;
  media_count: number;
  media_last: string | null;
  media_recent: MediaItem[];
  last_activity?: string | null;
  activity_basis?: string | null;
  users_count: number;
  bots_count?: number;
  posts_count?: number;
  posts_label?: string;
  messages_count?: number;
  messages_last?: string | null;
  follow_edges: { owner_account: string; followers: number; following: number }[];
  live?: string | null;
  age_seconds?: number | null;
  stale_after_seconds?: number | null;
  collection_mode?: string | null;
  freshness_basis?: string | null;
  health_detail?: string | null;
  source_health_status?: string | null;
  source_health_error?: string | null;
  whatsapp_bridge_health?: Record<string, unknown> | null;
}

export interface MessagingCoverageRow {
  network: string;
  native_source: string | null;
  beeper_network: string;
  canonical_source: "native" | "beeper";
  policy: string;
  native_messages: number;
  native_chats: number;
  native_people: number;
  native_last_message: string | null;
  beeper_messages: number;
  beeper_chats: number;
  beeper_people: number;
  beeper_last_message: string | null;
}
