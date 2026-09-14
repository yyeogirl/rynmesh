import type { RecommendationEvidencePacket, ReviewBasis } from "./types";

// Client for the node's Daily Digest API (/api/local/sources, /api/local/digest).
// Kept separate from NodeClient on purpose: the digest surface is live-node-only
// (no fixture variant), and nodeClient.ts carries in-flight owner edits.

export interface DigestSource {
  id: string;
  kind: string;
  feed_url: string;
  title: string;
  tags: string[];
  weight: number;
  builtin?: boolean;
  content_kind?: string;
}

export interface DigestSourceHealth {
  status?: "not_checked" | "healthy" | "cached" | "failed";
  id: string;
  title: string;
  ok: boolean;
  error: string;
  item_count: number;
  last_checked_unix: number;
  last_success_unix: number;
  consecutive_failures: number;
  using_cached_items: boolean;
}

export interface DigestItem {
  item_id: string;
  source_id: string;
  source_title: string;
  source_kind: string;
  title: string;
  link: string;
  summary: string;
  ai_summary: string;
  ai_summary_grounding_version: number;
  author: string;
  thumbnail: string;
  media_url: string;
  content_kind: string;
  content_type: string;
  tags: string[];
  published_unix: number;
  score: number;
  reasons: string[];
  review_basis: ReviewBasis;
  safety_outcome: "unscanned";
  provenance_status: "unsigned";
  evidence_packet: RecommendationEvidencePacket;
}

export interface DiscoveryStatus {
  phase: "waiting" | "refreshing" | "ready" | "error";
  message: string;
  last_started_unix: number;
  last_completed_unix: number;
  next_refresh_unix: number;
  new_items: number;
  unread_count: number;
  item_count: number;
  source_count: number;
  formats: string[];
  healthy_sources: number;
  failed_sources: number;
  cached_sources: number;
  degraded: boolean;
  offline_ready: boolean;
  source_health: DigestSourceHealth[];
}

export interface Digest {
  generated_at_unix: number;
  brief: string;
  ai: {
    provider: string;
    model: string;
    review_basis: ReviewBasis;
    grounding_version: number;
  } | null;
  items: DigestItem[];
  sources: DigestSourceHealth[];
}

export interface ConsumptionRecord {
  item_id: string;
  item: DigestItem;
  first_opened_unix: number;
  last_opened_unix: number;
  last_activity_unix: number;
  open_count: number;
  bookmarked: boolean;
  progress: number;
  completed: boolean;
  content_version?: string;
  sync_reading_available?: boolean;
  sync_revisions?: { reading?: string; bookmarks?: string };
  sync_conflicts?: { reading?: boolean; bookmarks?: boolean };
}

export interface Watcher {
  id: string;
  url: string;
  note: string;
  title: string;
}

export interface ReaderBlock {
  tag: string;
  text: string;
}

export interface ReaderArticle {
  truncated?: boolean;
  url: string;
  title: string;
  byline: string;
  lead_image: string;
  blocks: ReaderBlock[];
  word_count: number;
  cached: boolean;
}

export interface Steering {
  text: string;
  interests: string[];
  avoids: string[];
}

export interface AiStatus {
  provider: string | null;
  model: string | null;
}

export interface InstalledModel {
  name: string;
  size_bytes: number;
  modified: string;
}

export interface RecommendedModel {
  name: string;
  size_hint: string;
  tier: string;
  note: string;
  installed: boolean;
}

export interface LocalModelCatalog {
  ollama_running: boolean;
  installed: InstalledModel[];
  recommended: RecommendedModel[];
  /** what the node is using right now */
  current: string;
  /** the owner's explicit pick ("" = automatic) */
  selected: string;
  provider: string | null;
  anthropic_key_present: boolean;
}

export interface FeedbackSignal {
  event_id: string;
  content_id: string;
  title: string;
  action: "more" | "less" | "hide" | "neutral";
  tags: string[];
  publisher: string;
  platform: string;
  updated_at: string;
  undone_at: string;
  active: boolean;
  migrated: boolean;
}

export interface FeedbackHistory {
  items: FeedbackSignal[];
  total: number;
  offset: number;
  limit: number;
}

export class DigestClientError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

// Same base-URL rule as App.tsx resolveNodeBaseUrl: env override > Tauri
// loopback default > Vite dev proxy.
function baseUrl(): string {
  const explicit = import.meta.env.VITE_RYN_NODE_BASE_URL;
  if (explicit) return explicit;
  const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
  return isTauri ? "http://127.0.0.1:8791/api/local" : "/api/local";
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl()}${path}`, {
    // Carry the session cookie when the node is reached through a tunnel.
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    let detail = `Local Ryn node returned ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // keep the generic message
    }
    throw new DigestClientError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const digestApi = {
  feedbackHistory: (offset = 0) => requestJson<FeedbackHistory>(`/recommendations/signals?offset=${offset}&limit=20`),
  undoFeedback: (eventId: string) => requestJson<{ digest: Digest }>(
    `/recommendations/feedback/${encodeURIComponent(eventId)}/undo`, { method: "POST" },
  ),
  listSources: () => requestJson<DigestSource[]>("/sources"),
  sourceHealth: () => requestJson<DigestSourceHealth[]>("/sources/health"),
  retrySource: (sourceId: string) => requestJson<{ digest: Digest; status: DiscoveryStatus }>(
    `/sources/${encodeURIComponent(sourceId)}/retry`, { method: "POST" },
  ),
  addSource: (url: string) =>
    requestJson<DigestSource>("/sources", { method: "POST", body: JSON.stringify({ url }) }),
  removeSource: (sourceId: string) =>
    requestJson<{ ok: boolean }>(`/sources/${sourceId}`, { method: "DELETE" }),
  getDigest: () => requestJson<Digest>("/digest"),
  getDiscoveryStatus: () => requestJson<DiscoveryStatus>("/discovery/status"),
  markDiscoverySeen: () =>
    requestJson<DiscoveryStatus>("/discovery/seen", { method: "POST" }),
  refreshDigest: () =>
    requestJson<{ refresh: { new_items: number }; digest: Digest; status: DiscoveryStatus }>("/digest/refresh", {
      method: "POST",
    }),
  readArticle: (url: string) =>
    requestJson<ReaderArticle>(`/reader?url=${encodeURIComponent(url)}`),
  listConsumption: () => requestJson<ConsumptionRecord[]>("/consumption"),
  recordConsumption: (
    item: DigestItem,
    action: "opened" | "bookmark" | "unbookmark" | "progress" | "completed",
    progress?: number,
  ) => requestJson<ConsumptionRecord>("/consumption", {
    method: "POST",
    body: JSON.stringify({ item, action, progress }),
  }),
  clearConsumption: () => requestJson<{ ok: boolean }>("/consumption", { method: "DELETE" }),
  getSteering: () => requestJson<Steering>("/digest/steer"),
  steer: (text: string) =>
    requestJson<Steering>("/digest/steer", { method: "POST", body: JSON.stringify({ text }) }),
  sendFeedback: (itemId: string, action: "up" | "down" | "hide" | "opened" | "more_like_this") =>
    requestJson<{ ok: boolean }>("/digest/feedback", {
      method: "POST",
      body: JSON.stringify({ item_id: itemId, action }),
    }),
  saveReadLater: (url: string) =>
    requestJson<{ ok: boolean; title: string }>("/readlater", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  listWatchers: () => requestJson<Watcher[]>("/watchers"),
  addWatcher: (url: string, note: string) =>
    requestJson<Watcher>("/watchers", { method: "POST", body: JSON.stringify({ url, note }) }),
  removeWatcher: (watcherId: string) =>
    requestJson<{ ok: boolean }>(`/watchers/${watcherId}`, { method: "DELETE" }),
  aiStatus: () => requestJson<AiStatus>("/ai/status"),
  listModels: () => requestJson<LocalModelCatalog>("/ai/models"),
  selectModel: (model: string) =>
    requestJson<{ ok: boolean; selected: string; model: string | null }>("/ai/model", {
      method: "POST",
      body: JSON.stringify({ model }),
    }),
};
