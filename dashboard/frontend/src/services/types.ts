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

export interface FaceIdentity {
  id: string;
  label: string | null;
  occurrence_count: number;
  created_at: string;
  last_seen: string | null;
}

export interface WhatsAppUser {
  jid: string;
  push_name: string | null;
  display_name: string | null;
  phone_number: string | null;
  is_business: boolean;
  message_count: number;
  last_seen: string | null;
}

export interface UserHistoryEntry {
  id: number;
  user_jid: string;
  field_name: string;
  old_value: string;
  new_value: string;
  changed_at: string;
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
