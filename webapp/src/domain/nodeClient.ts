import type { ConsumptionRecord } from "./digestClient";
import type {
  ContentFilters,
  ContentBody,
  ContentItem,
  EgressStatus,
  FirstSuccessStatus,
  JobCapacity,
  NodeSettings,
  NodeStatus,
  Peer,
  PeerFilters,
  PeerHealth,
  PersonalDataExport,
  PrivacyEraseScope,
  PrivacyStatus,
  ProvenanceEvent,
  PublishDraft,
  PublishPrepResult,
  Recommendation,
  RecommendationFeedbackAction,
  RecommendationProfile,
  RegistryStatus,
  SearchAskResponse,
  UpdateStatus,
  WorkOrder,
  WorkResult,
} from "./types";

export interface LLMServiceRecord {
  network_id?: string;
  peer_id: string;
  node_name?: string;
  online: boolean;
  ready?: boolean;
  access?: "self" | "friend";
  capacity?: { available?: number; max_concurrent?: number; running?: number };
  benchmark?: { latency_ms?: number; tokens_per_second?: number };
  service: {
    package_id: string;
    model_alias: string;
    capabilities: string[];
    context_window: number;
    max_output_tokens: number;
    pricing: {
      currency: string;
      input_per_1k: number;
      output_per_1k: number;
      minimum: number;
      maximum_per_task: number;
    };
    privacy: { policy_text?: string; compute_node_sees_plaintext?: boolean };
    risk_labels?: string[];
  };
}

export interface LLMOrderResult {
  task_id: string;
  state: string;
  output?: string;
  model_alias?: string;
  input_tokens?: number;
  output_tokens?: number;
  duration_ms?: number;
  amount?: number;
  error_code?: string;
  created_at?: string;
  updated_at?: string;
  transport?: "local_runtime" | "peer_http_direct" | "ice_udp_direct" | "encrypted_relay" | "unknown";
  transport_evidence?: {
    relay_used?: boolean;
    public_nat_traversal_required?: boolean;
    distinct_public_egress_required?: boolean;
    peer_public_mapping_nominated?: boolean;
  };
}

export interface LLMPrivacySettings {
  result_retention_seconds: 0 | 3600 | 86400 | 604800;
  plaintext_persisted: boolean;
  stored_results_encrypted: boolean;
  compute_node_sees_plaintext: boolean;
}

export interface TaskBalanceSummary {
  currency: string;
  available: number;
  held: number;
  earned: number;
}

export interface LLMProviderStatus {
  configured?: boolean;
  ready?: boolean;
  online: boolean;
  service?: LLMServiceRecord["service"];
  capacity?: { available?: number; max_concurrent?: number; running?: number; queue_limit?: number };
  health?: Record<string, unknown>;
  publication_enabled?: boolean;
  accepting_orders?: boolean;
  network_id?: string;
  lifecycle?: {
    package_id?: string;
    mode?: string;
    runtime?: { managed?: boolean; installed?: boolean; running?: boolean; status?: string };
    health?: Record<string, unknown>;
    storage?: { model_owned: boolean; model_present: boolean | null; model_bytes: number | null };
    error?: string;
  };
}

export interface LLMSetupJob {
  job_id?: string;
  state: "idle" | "queued" | "running" | "cancelling" | "cancelled" | "failed" | "succeeded";
  stage: string;
  progress: number;
  message?: string;
  error_code?: string;
  retryable?: boolean;
  configured?: boolean;
  publication_enabled?: boolean;
  resume_configuration?: {
    mode: "managed";
    profile: "light" | "balanced" | "quality";
    package_id: string;
    port: number;
  } | null;
}

export interface LLMSetupRequest {
  mode: "managed" | "import-gguf" | "openai-compatible" | "ollama";
  package_id: string;
  alias: string;
  port?: number;
  model_path?: string;
  base_url?: string;
  model?: string;
  api_key_env?: string;
  allow_non_loopback?: boolean;
  accept_risk?: boolean;
  runtime?: "auto" | "native" | "docker";
  profile?: "auto" | "light" | "balanced" | "quality";
}

export interface LLMProfileRecommendation {
  profile: "light" | "balanced" | "quality";
  can_run: boolean;
  reason?: string;
  model_alias?: string;
  display_name?: string;
  parameter_millions?: number;
  quantization?: string;
  estimated_memory_mb?: number;
  estimated_disk_mb?: number;
  download_bytes?: number;
  source_url?: string;
  license_id?: string;
  license_notice?: string;
  license_url?: string;
  context_window?: number;
  max_concurrent?: number;
  recommended?: boolean;
}

export interface LLMHardwareReport {
  hardware: {
    native_runtime_available?: boolean;
    native_runtime_present?: boolean;
    [key: string]: unknown;
  };
  recommendations: LLMProfileRecommendation[];
}

export interface NodeClient {
  mode: "live" | "fixture";
  getNodeStatus(): Promise<NodeStatus>;
  getRegistryStatus(): Promise<RegistryStatus>;
  listJobCapacities(filters?: { capability?: string; network_id?: string }): Promise<JobCapacity[]>;
  submitWorkOrder(req: {
    provider_peer_id: string;
    capability: string;
    operation: string;
    params: Record<string, unknown>;
    max_credit_cost?: number;
    network_id?: string;
  }): Promise<{ work_order_id: string; order: WorkOrder }>;
  listWorkResults(filters?: { work_order_id?: string; status?: string; network_id?: string }): Promise<WorkResult[]>;
  listLLMServices(networkId?: string): Promise<LLMServiceRecord[]>;
  getLLMServiceStatus(): Promise<LLMProviderStatus>;
  publishLLMService(req?: { network_id?: string; benchmark?: boolean }): Promise<Record<string, unknown>>;
  pauseLLMService(): Promise<LLMProviderStatus>;
  setupLLMService(req: LLMSetupRequest): Promise<Record<string, unknown>>;
  startLLMSetup(req: LLMSetupRequest): Promise<LLMSetupJob>;
  getLLMSetupStatus(): Promise<LLMSetupJob>;
  cancelLLMSetup(jobId: string): Promise<LLMSetupJob>;
  getLLMHardware(): Promise<LLMHardwareReport>;
  runLLMServiceAction(
    action: "start" | "stop" | "restart" | "update" | "self-test" | "uninstall",
    options?: { delete_environment?: boolean; delete_model?: boolean; confirm_model_delete?: boolean },
  ): Promise<Record<string, unknown>>;
  getTaskBalance(): Promise<TaskBalanceSummary>;
  submitLLMOrder(req: {
    task_id?: string;
    idempotency_key?: string;
    network_id?: string;
    provider_peer_id: string;
    service_id: string;
    prompt: string;
    max_tokens: number;
    transport?: "auto" | "direct" | "p2p" | "relay";
  }): Promise<LLMOrderResult>;
  getLLMOrder(taskId: string): Promise<LLMOrderResult>;
  cancelLLMOrder(taskId: string): Promise<LLMOrderResult>;
  listLLMOrders(): Promise<LLMOrderResult[]>;
  getLLMPrivacy(): Promise<LLMPrivacySettings>;
  updateLLMPrivacy(retentionSeconds: LLMPrivacySettings["result_retention_seconds"]): Promise<LLMPrivacySettings>;
  clearLLMOrders(): Promise<{ ok: boolean; removed: number }>;
  discoverPeers(req?: { network?: string }): Promise<Peer[]>;
  listPeers(filters?: PeerFilters): Promise<Peer[]>;
  listContent(filters?: ContentFilters): Promise<ContentItem[]>;
  getContentItem(contentId: string): Promise<ContentItem>;
  getContentBody(contentId: string): Promise<ContentBody>;
  getProvenance(headHash: string | null): Promise<ProvenanceEvent[]>;
  fetchPreview(contentId: string, providerPeerId: string): Promise<{ ok: boolean; size: string }>;
  fetchFullContent(contentId: string, providerPeerId: string): Promise<{ ok: boolean; size: string }>;
  requestRecommendations(req?: { query?: string; limit?: number }): Promise<Recommendation[]>;
  getFirstSuccess(): Promise<FirstSuccessStatus>;
  dismissFirstSuccess(): Promise<FirstSuccessStatus>;
  resetFirstSuccess(): Promise<FirstSuccessStatus>;
  recordContentConsumption(
    item: ContentItem,
    action: "opened" | "bookmark" | "unbookmark" | "completed" | "progress",
    progress?: number,
    reading?: { content_version: string; expected_sync_revision: string },
  ): Promise<ConsumptionRecord | void>;
  getRecommendationProfile(): Promise<RecommendationProfile>;
  updateRecommendationProfile(
    patch: Partial<Pick<RecommendationProfile, "direction" | "topics" | "platforms">>,
  ): Promise<RecommendationProfile>;
  submitRecommendationFeedback(
    contentId: string,
    action: RecommendationFeedbackAction,
  ): Promise<RecommendationProfile>;
  submitSearchAsk(req: { text: string }): Promise<SearchAskResponse>;
  preparePublish(req: PublishDraft): Promise<PublishPrepResult>;
  confirmPublish(draftId: string): Promise<{ ok: boolean; content_id: string }>;
  getCreditScoreboard(
    filters?: { peerId?: string },
  ): Promise<{ peers: Array<Pick<Peer, "id" | "name" | "credits" | "weight">> }>;
  getSettings(): Promise<NodeSettings>;
  updateSettings(patch: Partial<NodeSettings>): Promise<NodeSettings>;
  getActivity(): Promise<import("./types").ActivityEvent[]>;
  getPrivacyStatus(): Promise<PrivacyStatus>;
  exportPersonalData(): Promise<PersonalDataExport>;
  erasePersonalData(scopes: PrivacyEraseScope[]): Promise<{ ok: boolean; erased: string[] }>;
  updatesStatus(): Promise<UpdateStatus>;
  updatesCheck(): Promise<UpdateStatus>;
  updatesApply(): Promise<{ applied: boolean; version?: string; error?: string }>;
  setAutoUpdate(on: boolean): Promise<void>;
  peersHealth(): Promise<PeerHealth[]>;
  egressStatus(region?: string): Promise<EgressStatus>;
  egressConnect(req?: { region?: string; provider_peer_id?: string }): Promise<EgressStatus>;
  egressLaunch(
    req?: { region?: string; urls?: string[] },
  ): Promise<{ launched?: boolean; count?: number; lastError?: string | null }>;
  egressDisconnect(req?: { region?: string }): Promise<EgressStatus>;
  listMessages(peerId: string): Promise<any[]>;
  sendMessage(req: { peer_id: string; text?: string; attachment?: { filename: string; mime: string; bytes_b64: string } }): Promise<any>;
  messagesStreamUrl(): string;
}

export class NodeClientError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = "NodeClientError";
  }
}
