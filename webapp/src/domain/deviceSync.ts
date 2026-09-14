import { nodeControlUrl } from "./nodeUrl";

export type SyncScope = "bookmarks" | "reading" | "conversations";
export const syncScopes: SyncScope[] = ["bookmarks", "reading", "conversations"];
export const scopeNames: Record<SyncScope, string> = { bookmarks: "Saved content", reading: "Reading progress", conversations: "Ask Ryn history" };
export type DeviceIdentity = { name: string; peer_id: string; actor: string; endpoint: string };
export type ReadingConflict = { id: string; scope: "reading" | "bookmarks"; revision: string;
  item: { title?: string; source_title?: string } | null;
  candidates: { choice_id: string; value: { progress?: number; completed?: boolean; content_version?: string; bookmarked?: boolean } | null }[] };
export type DeviceInvite = { id: string; device: DeviceIdentity; scopes: SyncScope[]; created: number; expires: number };
export type DevicePair = { id: string; role: "inviter" | "joiner"; status: string; device: DeviceIdentity;
  review_token: string; verification_code: string; expires: number; scopes: SyncScope[]; remote_scopes: SyncScope[];
  paused: boolean; remote_paused: boolean; revision: number; effective_scopes: SyncScope[]; removal_pending: boolean;
  sync?: { state: string; pending: number | null; last_success_at: number | null; error_code: string; conflicts: number } };
export type DeviceStatus = { pairing_available: boolean; reason: string | null; data_transfer_available: boolean;
  devices: DevicePair[]; invites: (Omit<DeviceInvite, "device"> & { status: string; pair_id: string | null })[] };
export const pairLabels: Record<string, string> = { awaiting_owner: "Review on this device", awaiting_inviter: "Waiting for the other device to approve",
  awaiting_peer: "Waiting for the other device to confirm", awaiting_ack: "Waiting for confirmation receipt",
  active: "Pairing confirmed", expired: "Invitation expired", rejected: "Pairing not accepted", revoked: "Device removed" };
const errors: Record<string, string> = {
  sync_endpoint_unavailable: "This device needs a reachable network address before you can create or accept a new invitation. Check Network settings.",
  sync_invite_expired: "This invitation expired. Create a new invitation on the other device.",
  sync_invite_invalid: "This device invitation is incomplete or invalid. Paste the complete invitation again.",
  sync_cannot_pair_self: "This invitation belongs to this device. Open it on your other computer.",
  sync_device_already_paired: "This device is already paired. Review its existing entry below.",
  sync_revision_conflict: "The device settings changed. Refresh and review them again before saving.",
  sync_pairing_review_changed: "The pairing request changed. Refresh and review the identity and scope again.",
  sync_pairing_not_pending: "This request is no longer waiting for approval. Refresh its status.",
  sync_device_not_active: "This device is not paired or its access was removed. Review its status below.",
  sync_scope_denied: "The selected scope is not allowed by both devices. Review their choices.",
  sync_version_unsupported: "This device data needs a newer app version. Existing records have been kept.",
  sync_pairing_capacity_exhausted: "The device invitation storage limit was reached. Existing records have been kept.",
  sync_reading_not_conflicted: "This reading change has already been resolved. Refresh to review the current position.",
  sync_reading_choice_invalid: "This choice is no longer available. Refresh and review the current candidates.",
};
async function request<T>(path = "", method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(nodeControlUrl(`/device-sync${path}`), { method, credentials: "include",
    headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new Error(errors[value.detail] ?? "This operation could not be confirmed. Refresh the device list and retry when connected.");
  }
  return response.json();
}
const devicePath = (id: string, action: string) => `/devices/${encodeURIComponent(id)}/${action}`;
export const deviceSyncApi = {
  readingConflicts: () => request<{ conflicts: ReadingConflict[]; local_actor: string }>("/reading/conflicts"),
  resolveReading: (issue: ReadingConflict, choice_id: string) => request("/reading/resolve", "POST",
    { id: issue.id, scope: issue.scope, expected_revision: issue.revision, choice_id }),
  status: () => request<DeviceStatus>(),
  invite: (scopes: SyncScope[]) => request<{ uri: string; invite: DeviceInvite }>("/invites", "POST", { scopes }),
  inspect: (uri: string) => request<DeviceInvite>("/invites/inspect", "POST", { uri }),
  join: (uri: string, scopes: SyncScope[]) => request<DevicePair>("/join", "POST", { uri, scopes }),
  cancel: (id: string) => request(`/invites/${encodeURIComponent(id)}`, "DELETE"),
  approve: (pair: DevicePair, scopes: SyncScope[]) => request<DevicePair>(devicePath(pair.id, "approve"), "POST", { review_token: pair.review_token, scopes }),
  configure: (pair: DevicePair, scopes: SyncScope[], paused: boolean) => request<DevicePair>(devicePath(pair.id, "policy"), "PUT", { expected_revision: pair.revision, scopes, paused }),
  remove: (pair: DevicePair) => request<DevicePair>(devicePath(pair.id, "remove"), "POST", { expected_revision: pair.revision }),
  retry: (id: string) => request<DevicePair>(devicePath(id, "retry"), "POST"),
};
