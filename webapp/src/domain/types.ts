export type IdentityTier = "unverified" | "attested" | "staked" | "proven";
export type SafetyOutcome = "passed" | "pending" | "flagged" | "blocked" | "unscanned";
export type ProvenanceStatus = "signed" | "partial" | "unsigned" | "broken";
export type FetchStatus =
  | "local"
  | "local_draft"
  | "fetched_full"
  | "preview_only"
  | "discovered"
  | "fetching";
export type ContentKind =
  | "video"
  | "image"
  | "audio"
  | "document"
  | "slides"
  | "dataset"
  | "code"
  | "report"
  | "package"
  | "model";
export type ReviewBasis = "metadata" | "preview" | "full";
export type ActionRisk = "low" | "medium" | "high";

export interface Peer {
  id: string;
  slug: string;
  name: string;
  endpoint: string;
  network: string;
  tier: IdentityTier;
  credits: number;
  weight: number;
  lastSeen: string;
  served: number;
  fetched: number;
  quarantined?: boolean;
  trustedRoot?: boolean;
  color?: string;
  isSelf?: boolean;
}

export interface ContentItem {
  content_id: string;
  manifest_hash: string;
  title: string;
  description: string;
  tags: string[];
  content_kind: ContentKind;
  content_type: string;
  publisher_peer_id: string;
  provider_peer_id: string;
  source_peer_name?: string;
  identity_tier?: IdentityTier;
  credit_score?: number;
  distribution_weight: number;
  safety_outcome: SafetyOutcome;
  safety_scanner_id?: string;
  safety_notes?: string;
  provenance_status: ProvenanceStatus;
  provenance_head_hash: string | null;
  fetch_status: FetchStatus;
  review_basis: ReviewBasis;
  size?: string;
  novelty?: number;
  ai_score?: number;
  published: string | null;
  updated?: string;
  source_platform?: string;
  starter?: boolean;
  external?: boolean;
  external_url?: string;
  thumbnail_url?: string;
  media_url?: string;
  digest_item_id?: string;
}

export interface ContentBody {
  ok: boolean;
  content_id: string;
  content_type: string;
  size: string;
  truncated: boolean;
  text: string;
}

export interface ProvenanceEvent {
  t: string;
  label: string;
  actor: string;
  kind: "publish" | "scan" | "render" | "edit" | "cite" | "generate";
  hash: string;
  notes?: string;
  flagged?: boolean;
}

export type RecommendationEvidence =
  | "content_match"
  | "publisher_match"
  | "peer_trust"
  | "peer_reputation"
  | "query_match"
  | "tag_match"
  | "diversity"
  | "safety_passed"
  | "provenance_signed";

export interface RecommendationEvidencePacket {
  version: number;
  content_id: string;
  review_basis: ReviewBasis;
  reviewed_at_unix: number;
  source: { name: string; platform: string; url: string };
  signals: { kind: string; label: string }[];
  observations: { field: string; label: string; value: string }[];
  citations: { kind: string; label: string; url: string }[];
  limitations: string[];
}

export interface Recommendation {
  id: string;
  contentId: string;
  priority: number;
  reason: string;
  evidence: RecommendationEvidence[];
  novelty: string | null;
  uncertainty: string | null;
  review_basis: ReviewBasis;
  evidence_packet: RecommendationEvidencePacket;
  item?: ContentItem;
}

export type RecommendationFeedbackAction = "more" | "less" | "hide" | "neutral";

export interface RecommendationChoice {
  id: string;
  label: string;
  tags?: string[];
}

export interface RecommendationProfile {
  version: number;
  direction: string;
  topics: string[];
  platforms: string[];
  feedback: Record<string, { action: RecommendationFeedbackAction }>;
  updated_at: string;
  topic_choices: RecommendationChoice[];
  platform_choices: RecommendationChoice[];
  learned_signals: number;
  feedback_count: number;
}

export type FirstSuccessPhase =
  | "checking_sources"
  | "needs_action"
  | "ready"
  | "awaiting_signal"
  | "completed";

export interface FirstSuccessStatus {
  version: "ryn.first-success.v1";
  phase: FirstSuccessPhase;
  completed: boolean;
  dismissed: boolean;
  node_ready: boolean;
  content_ready: boolean;
  item_count: number;
  healthy_sources: number;
  source_count: number;
  failed_sources: number;
  degraded: boolean;
  using_cache: boolean;
  first_item_opened: boolean;
  first_signal_recorded: boolean;
  milestones: Record<string, number>;
  safe_error: "discovery_unavailable" | null;
  recoverable_actions: string[];
}

export interface NodeStatus {
  node_name: string;
  peer_id: string;
  daemon_running: boolean;
  desktop_managed?: boolean;
  registry: "connected" | "disconnected" | "unknown";
  peer_count: number;
  local_items: number;
  fetched_items: number;
  pending_recs: number;
  version: string;
  uptime_seconds: number;
}

export interface ActivityEvent {
  t: string;
  kind: "fetch" | "scan" | "credit" | "rec" | "peer" | "verify" | "publish" | "flag";
  text: string;
  itemId?: string;
  id?: string;
  timestamp_unix?: number;
  details?: Record<string, unknown>;
}

export interface PrivacyStatus {
  storage_root: string;
  reading_history_items: number;
  feedback_items: number;
  learned_signals: number;
  cached_discovery_items: number;
  reader_cache_files: number;
  audit_events: number;
  cloud_ai_enabled: boolean;
}

export type PrivacyEraseScope = "history" | "profile" | "cache" | "audit" | "onboarding";
export type PersonalDataExport = Record<string, unknown> & {
  version: number;
  exported_at: string;
  storage: "local";
};

export interface PublishDraft {
  path: string;
  kind: ContentKind;
  title: string;
  description: string;
  tags: string[];
  model_used?: string;
  visibility: "network" | "trusted" | "local";
}

export interface PublishPrepResult {
  draft_id: string;
  content_hash: string;
  preview_hash: string;
  manifest_hash: string;
  safety: { outcome: SafetyOutcome; scanner: string; notes?: string };
  provenance_head: string;
}

export interface NodeSettings {
  node_name: string;
  node_storage: string;
  peer_http_host: string;
  peer_http_port: number;
  public_endpoint: string;
  registry_url: string;
  trusted_roots: string[];
  safety_policy: "permissive" | "standard" | "strict";
  ai_provider: "local" | "cloud";
  ai_model: string;
  cloud_access: boolean;
  rank_default: "weight" | "newest" | "trusted" | "ai" | "novelty";
  publish_visibility: "network" | "trusted" | "local";
  fetch_budget_mb: number;
  fetch_used_mb: number;
  fetch_timeout_s: number;
  onboarding_version: number;
  notifications_enabled: boolean;
  notification_frequency: "immediate" | "hourly" | "daily";
  notification_quiet_start: number;
  notification_quiet_end: number;
  desktop_managed?: boolean;
  network_id?: string;
}

export interface SearchAskRoutingOp {
  name: string;
  risk: ActionRisk;
  status: "pending" | "done" | "failed";
}

export interface SearchAskResponse {
  routing: { text: string; operations: SearchAskRoutingOp[] };
  assistant: {
    text: string;
    cites: string[];
    suggests: string[];
  };
}

export interface RegistryStatus {
  status: "connected" | "disconnected";
  url: string;
}

export interface JobCapacity {
  peer_id: string;
  node_name: string;
  provider_name?: string;
  capabilities: string[];
  network_id: string;
  capacity_units: number;
  max_concurrent: number;
  price_credits: Record<string, number>;
  polling_interval_sec: number;
  metadata: Record<string, unknown>;
  updated_at: string;
}

export interface WorkOrder {
  work_order_id: string;
  requester_peer_id: string;
  provider_peer_id: string;
  capability: string;
  operation: string;
  params: Record<string, unknown>;
  network_id: string;
  max_credit_cost: number;
  created_at: string;
  expires_at: string;
}

export interface WorkResult {
  work_order_id: string;
  provider_peer_id: string;
  requester_peer_id: string;
  status: "open" | "accepted" | "running" | "completed" | "failed" | "cancelled";
  message: string;
  result_refs: Record<string, unknown>;
  credit_amount: number;
  network_id: string;
  created_at: string;
}

export interface ContentFilters {
  source?: "all" | "local" | "fetched" | "discovered" | string;
  kind?: ContentKind | "all";
  safety?: SafetyOutcome | "all";
  tier?: IdentityTier | "all";
  provenance?: ProvenanceStatus | "all";
  search?: string;
  rank?: NodeSettings["rank_default"];
}

export interface PeerFilters {
  tier?: IdentityTier | "all";
  quarantined?: boolean;
  search?: string;
}

export interface ConversationMessage {
  id: string;
  role: "user" | "system" | "assistant";
  text: string;
  operations?: SearchAskRoutingOp[];
  cites?: string[];
  suggests?: string[];
}

export interface ConfirmRequest {
  title: string;
  body: string;
  risk: ActionRisk;
  confirmLabel?: string;
  details?: Array<{ label: string; value: string }>;
  onConfirm: () => void | Promise<void>;
}

export interface ToastMessage {
  id: string;
  tone: "ok" | "info" | "warn" | "danger";
  text: string;
}

export type EgressRegion = "CN" | "HK";

export interface EgressStatus {
  region: string;
  connected: boolean;
  providerPeerId: string;
  providerNodeName: string;
  socksPort: number | null;
  exitIp: string | null;
  loc: string;
  locVerified: boolean;
  ttlExpiresAt: string;
  priceCredits: number;
  lastError: string | null;
  connectedAt: string | null;
  uptimeSeconds: number | null;
}

export interface PeerHealth {
  peerId: string;
  online: boolean;
  checkedAt: string;
}

export interface UpdateStatus {
  currentVersion: string;
  availableVersion: string | null;
  autoUpdate: boolean;
  lastCheck: string | null;
  lastError: string | null;
}
