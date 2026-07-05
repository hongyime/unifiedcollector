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
