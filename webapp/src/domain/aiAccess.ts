import { nodeControlUrl } from "./nodeUrl";
import type { LLMServiceRecord } from "./nodeClient";

export interface AIGrant {
  service_id: string; relationship_id: string; peer_id: string;
  allowed: boolean; effective: boolean; revision: number;
}
export interface FriendAISnapshot {
  peer_id: string; relationship_id: string; checked_at: number;
  status: "authorized" | "not_authorized" | "revoked" | "service_unavailable" | "stale"; services: LLMServiceRecord[];
}
async function request<T>(path = "", method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(nodeControlUrl(`/ai-access${path}`), { method, credentials: "include",
    headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    const messages: Record<string, string> = {
      ai_permission_revision_conflict: "Permission changed in another view. Refresh before making another choice.",
      ai_friend_inactive: "This friendship is no longer active. Refresh your friends list.",
      ai_friend_service_unreachable: "Could not reach this friend's AI service. Check the connection and refresh; access was not confirmed.",
    };
    throw new Error(messages[value.detail] ?? "Could not confirm the AI permission change. Refresh to check the current state before retrying.");
  }
  return response.json() as Promise<T>;
}
export const aiAccess = {
  list: () => request<{ grants: AIGrant[] }>(),
  set: (service: string, relationship: string, allowed: boolean, revision: number) =>
    request<{ grant: AIGrant; cancellation: string }>(`/${encodeURIComponent(service)}/${encodeURIComponent(relationship)}`, "PUT", { allowed, expected_revision: revision }),
  friends: () => request<{ friends: FriendAISnapshot[] }>("/friend-services"),
  refresh: (peer: string) => request<FriendAISnapshot>(`/friend-services?${new URLSearchParams({ peer_id: peer })}`, "POST"),
};
