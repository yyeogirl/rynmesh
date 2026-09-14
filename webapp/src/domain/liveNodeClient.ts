import type { NodeClient } from "./nodeClient";
import type { ConsumptionRecord } from "./digestClient";
import { NodeClientError } from "./nodeClient";
import type {
  ContentFilters,
  NodeSettings,
  PeerFilters,
  PublishDraft,
  PrivacyEraseScope,
} from "./types";

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    // Carry the session cookie when the node is reached through a tunnel.
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail.trim();
    } catch {
      // The status code remains useful when the response body is not JSON.
    }
    throw new NodeClientError(
      `Local Ryn node returned ${response.status}${detail ? `: ${detail}` : ""}`,
      response.status,
    );
  }
  return (await response.json()) as T;
}

function qs<T extends object>(value: T | undefined): string {
  if (!value) return "";
  const params = new URLSearchParams();
  for (const [key, raw] of Object.entries(value)) {
    if (raw !== undefined && raw !== "" && raw !== "all") params.set(key, String(raw));
  }
  const out = params.toString();
  return out ? `?${out}` : "";
}

export function makeLiveNodeClient(baseUrl = "/api/local"): NodeClient {
  return {
    mode: "live",
    getNodeStatus: () => requestJson(`${baseUrl}/node/status`),
    getRegistryStatus: () => requestJson(`${baseUrl}/registry/status`),
    listJobCapacities: (filters) => requestJson(`${baseUrl}/jobs/capacity${qs(filters)}`),
    submitWorkOrder: (req) =>
      requestJson(`${baseUrl}/jobs/work-orders`, { method: "POST", body: JSON.stringify(req) }),
    listWorkResults: async (filters) => {
      const payload = await requestJson<{ work_results: import("./types").WorkResult[] }>(
        `${baseUrl}/jobs/work-results${qs(filters)}`,
      );
      return payload.work_results;
    },
    listLLMServices: async (networkId = "rynmesh-main") => {
      const payload = await requestJson<{ services: import("./nodeClient").LLMServiceRecord[] }>(
        `${baseUrl}/llm/services${qs({ network_id: networkId })}`,
      );
      return payload.services;
    },
    getLLMServiceStatus: () => requestJson(`${baseUrl}/llm/service/status`),
    publishLLMService: (req) =>
      requestJson(`${baseUrl}/llm/services/publish`, {
        method: "POST",
        body: JSON.stringify(req ?? {}),
      }),
    pauseLLMService: () => requestJson(`${baseUrl}/llm/services/pause`, { method: "POST" }),
    setupLLMService: (req) =>
      requestJson(`${baseUrl}/llm/setup`, { method: "POST", body: JSON.stringify(req) }),
    startLLMSetup: (req) =>
      requestJson(`${baseUrl}/llm/setup/async`, { method: "POST", body: JSON.stringify(req) }),
    getLLMSetupStatus: () => requestJson(`${baseUrl}/llm/setup/status`),
    cancelLLMSetup: (jobId) =>
      requestJson(`${baseUrl}/llm/setup/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }),
    getLLMHardware: () => requestJson(`${baseUrl}/llm/hardware`),
    runLLMServiceAction: (action, options) =>
      requestJson(`${baseUrl}/llm/service/actions/${encodeURIComponent(action)}`, {
        method: "POST",
        body: JSON.stringify(options ?? {}),
      }),
    getTaskBalance: () => requestJson(`${baseUrl}/task-balance`),
    submitLLMOrder: (req) =>
      requestJson(`${baseUrl}/llm/orders/async`, { method: "POST", body: JSON.stringify(req) }),
    getLLMOrder: (taskId) =>
      requestJson(`${baseUrl}/llm/orders/${encodeURIComponent(taskId)}`),
    cancelLLMOrder: (taskId) =>
      requestJson(`${baseUrl}/llm/orders/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }),
    listLLMOrders: async () => {
      const payload = await requestJson<{ orders: import("./nodeClient").LLMOrderResult[] }>(`${baseUrl}/llm/orders`);
      return payload.orders;
    },
    getLLMPrivacy: () => requestJson(`${baseUrl}/llm/privacy`),
    updateLLMPrivacy: (resultRetentionSeconds) =>
      requestJson(`${baseUrl}/llm/privacy`, {
        method: "PUT",
        body: JSON.stringify({ result_retention_seconds: resultRetentionSeconds }),
      }),
    clearLLMOrders: () => requestJson(`${baseUrl}/llm/orders`, { method: "DELETE" }),
    discoverPeers: (req) =>
      requestJson(`${baseUrl}/peers/discover`, { method: "POST", body: JSON.stringify(req ?? {}) }),
    listPeers: (filters?: PeerFilters) => requestJson(`${baseUrl}/peers${qs(filters)}`),
    listContent: (filters?: ContentFilters) => requestJson(`${baseUrl}/content${qs(filters)}`),
    getContentItem: (contentId: string) => requestJson(`${baseUrl}/content/${contentId}`),
    getContentBody: (contentId: string) =>
      requestJson(`${baseUrl}/content/${encodeURIComponent(contentId)}/body`),
    getProvenance: (headHash: string | null) =>
      requestJson(`${baseUrl}/provenance/${headHash ?? "none"}`),
    fetchPreview: (contentId: string, providerPeerId: string) =>
      requestJson(`${baseUrl}/content/${contentId}/fetch-preview`, {
        method: "POST",
        body: JSON.stringify({ providerPeerId }),
      }),
    fetchFullContent: (contentId: string, providerPeerId: string) =>
      requestJson(`${baseUrl}/content/${contentId}/fetch-full`, {
        method: "POST",
        body: JSON.stringify({ providerPeerId }),
      }),
    requestRecommendations: (req) =>
      requestJson(`${baseUrl}/recommendations`, { method: "POST", body: JSON.stringify(req ?? {}) }),
    getFirstSuccess: () => requestJson(`${baseUrl}/first-success`),
    dismissFirstSuccess: () => requestJson(`${baseUrl}/first-success/dismiss`, { method: "POST" }),
    resetFirstSuccess: () => requestJson(`${baseUrl}/first-success/reset`, { method: "POST" }),
    recordContentConsumption: async (item, action, progress, reading) => {
      return requestJson<ConsumptionRecord>(`${baseUrl}/consumption`, {
        method: "POST",
        body: JSON.stringify({
          action,
          progress,
          ...reading,
          item: {
            item_id: item.digest_item_id ?? item.content_id,
            source_id: item.publisher_peer_id,
            source_title: item.source_peer_name ?? item.source_platform ?? "Ryn source",
            source_kind: item.source_platform ?? "rynmesh",
            title: item.title,
            link: item.external_url || `rynmesh://content/${encodeURIComponent(item.content_id)}`,
            summary: item.description,
            content_kind: item.content_kind,
            content_type: item.content_type,
            tags: item.tags,
            reasons: [],
          },
        }),
      });
    },
    getRecommendationProfile: () => requestJson(`${baseUrl}/recommendations/profile`),
    updateRecommendationProfile: (patch) =>
      requestJson(`${baseUrl}/recommendations/profile`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      }),
    submitRecommendationFeedback: (contentId, action) =>
      requestJson(`${baseUrl}/recommendations/feedback`, {
        method: "POST",
        body: JSON.stringify({ contentId, action }),
      }),
    submitSearchAsk: (req) =>
      requestJson(`${baseUrl}/search-ask`, { method: "POST", body: JSON.stringify(req) }),
    preparePublish: (req: PublishDraft) =>
      requestJson(`${baseUrl}/publish/prepare`, { method: "POST", body: JSON.stringify(req) }),
    confirmPublish: (draftId: string) =>
      requestJson(`${baseUrl}/publish/${draftId}/confirm`, { method: "POST" }),
    getCreditScoreboard: (filters?: { peerId?: string }) =>
      requestJson(`${baseUrl}/credits/scoreboard${qs(filters)}`),
    getSettings: () => requestJson(`${baseUrl}/settings`),
    updateSettings: (patch: Partial<NodeSettings>) =>
      requestJson(`${baseUrl}/settings`, { method: "PATCH", body: JSON.stringify(patch) }),
    getActivity: () => requestJson(`${baseUrl}/activity`),
    getPrivacyStatus: () => requestJson(`${baseUrl}/privacy/status`),
    exportPersonalData: () => requestJson(`${baseUrl}/privacy/export`),
    erasePersonalData: (scopes: PrivacyEraseScope[]) =>
      requestJson(`${baseUrl}/privacy/erase`, {
        method: "POST",
        body: JSON.stringify({ scopes }),
      }),
    updatesStatus: () => requestJson(`${baseUrl}/updates/status`),
    updatesCheck: () => requestJson(`${baseUrl}/updates/check`, { method: "POST" }),
    updatesApply: () => requestJson(`${baseUrl}/updates/apply`, { method: "POST" }),
    setAutoUpdate: (on) =>
      requestJson(`${baseUrl}/settings`, { method: "PATCH", body: JSON.stringify({ auto_update: on }) }).then(() => undefined),
    peersHealth: () => requestJson(`${baseUrl}/peers/health`, { method: "POST" }),
    egressStatus: (region = "CN") => requestJson(`${baseUrl}/egress/status${qs({ region })}`),
    egressConnect: (req) =>
      requestJson(`${baseUrl}/egress/connect`, { method: "POST", body: JSON.stringify(req ?? {}) }),
    egressLaunch: (req) =>
      requestJson(`${baseUrl}/egress/launch`, { method: "POST", body: JSON.stringify(req ?? {}) }),
    egressDisconnect: (req) =>
      requestJson(`${baseUrl}/egress/disconnect`, { method: "POST", body: JSON.stringify(req ?? {}) }),
    listMessages: (peerId) => requestJson(`${baseUrl}/messages?peer_id=${encodeURIComponent(peerId)}`),
    sendMessage: (req) =>
      requestJson(`${baseUrl}/messages/send`, { method: "POST", body: JSON.stringify(req) }),
    messagesStreamUrl: () => `${baseUrl}/messages/stream`,
  };
}
