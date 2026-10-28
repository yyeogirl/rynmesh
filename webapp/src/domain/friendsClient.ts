import { nodeControlUrl } from "./nodeUrl";
import type { FriendContentCard, FriendInvitePreview, FriendInviteResult, FriendMessage, FriendRecord } from "./friendTypes";

const explanations: Record<string, string> = {
  friend_card_erased: "This card was cleared locally. It cannot be restored by retrying the old share.",
  friend_card_cleanup_review_changed: "Card history or a remaining legacy file changed. Review again before clearing it.",
  friend_card_cleanup_file_unavailable: "A legacy card file could not be safely read. Current data is kept; repair or restore that file before retrying.",
  friend_card_cleanup_version_unsupported: "This card-cleanup format needs a newer Ryn version. Data has been kept.",
  friend_card_cleanup_limit: "The node's 10,000 retained deletion identifiers are full. Existing deletion protection is kept; no additional cards were cleared.",
  invite_expired: "This invite expired. Ask your friend for a new invite.",
  invite_used: "Someone else already used this invite. Ask for a new one.",
  invite_cancelled: "Your friend cancelled this invite. Ask for a new one.",
  invalid_invite: "This invite is incomplete or invalid. Paste the whole invite again.",
  unsafe_endpoint: "This invite has an unsupported address. Ask your friend to check their connection.",
  could_not_join_friend: "Could not reach your friend. Keep this invite and retry when they are reachable.",
  friend_revoked: "This relationship was removed. Create a new invitation to reconnect.",
  active_friend_required: "This friend no longer has access. Refresh your friends list.",
  attachment_too_large: "This attachment exceeds the 5 MiB limit. Choose a smaller file.",
  message_id_conflict: "This send attempt already contains different content. Refresh the conversation before sending again.",
  friend_queue_full: "The outgoing queue is full. Wait for pending messages before sending more.",
  friend_card_read_first: "Open this article again to prepare a local copy, then retry sharing.",
  friend_card_content_too_large: "This document exceeds the 5 MiB sharing limit.",
  friend_card_content_unavailable: "This document is not available locally. Open it and retry sharing.",
  friend_card_content_changed: "This offline copy changed or was cleared. Reopen the current copy and review it before sharing.",
  friend_card_id_conflict: "This share attempt belongs to different content. Reopen the share dialog.",
  friend_copy_unavailable: "This saved copy is missing or damaged. Choose Download again to restore it from your friend.",
  library_import_cancelled_by_cleanup: "Local copies were cleared while this download was running. Nothing was restored. Start a new download if you want this copy back.",
  library_import_version_unsupported: "This saved data needs a newer version of Ryn. It has been kept unchanged.",
  library_cleanup_pending: "Document cleanup is unfinished. Continue it in Settings → Privacy & data before downloading this copy again.",
};

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(nodeControlUrl(`/friends${path}`), {
    method, credentials: "include", headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(explanations[detail.detail ?? ""] ?? "The operation could not be confirmed. Check your connection and retry.");
  }
  return response.json() as Promise<T>;
}

export const friendsApi = {
  list: () => request<{ friends: FriendRecord[] }>(""),
  invites: () => request<{ invites: (FriendInvitePreview & { status: string })[] }>("/invites"),
  createInvite: () => request<FriendInviteResult>("/invites", "POST", { ttl_minutes: 15 }),
  inspect: (invite_uri: string) => request<FriendInvitePreview>("/invites/inspect", "POST", { invite_uri }),
  join: (invite_uri: string) => request<FriendRecord>("/join", "POST", { invite_uri }),
  cancel: (id: string) => request(`/invites/${encodeURIComponent(id)}`, "DELETE"),
  revoke: (id: string) => request(`/${encodeURIComponent(id)}`, "DELETE"),
  retryRevocation: (id: string) => request(`/${encodeURIComponent(id)}/retry-revocation`, "POST"),
  history: (peer: string) => request<{ messages: FriendMessage[] }>(`/${encodeURIComponent(peer)}/messages`),
  send: (peer: string, body: { message_id: string; text: string; attachment?: { filename: string; mime: string; data_base64: string } }) =>
    request<FriendMessage>(`/${encodeURIComponent(peer)}/messages`, "POST", body),
  retry: (peer: string) => request(`/${encodeURIComponent(peer)}/retry-messages`, "POST"),
  cards: () => request<{ cards: FriendContentCard[] }>("/cards"),
  reviewCardCleanup: () => request<{ review_token: string; cards: number; legacy_files: number; resuming: boolean }>("/card-cleanup"),
  clearCards: (review_token: string) => request<{ cards: number; complete: boolean; remote_confirmed: boolean }>("/card-cleanup", "POST", { review_token }),
  share: (body: { peer_id: string; item_id: string; card_id: string; offline_job_id?: string }) => request<FriendContentCard>("/share", "POST", { ...body, prefer_source: !body.offline_job_id }),
  fetchCard: (id: string, repair = false) => request<{ library_id: string; sha256_verified: boolean }>(`/cards/${encodeURIComponent(id)}/fetch`, "POST", { repair }),
  retryCard: (id: string) => request<FriendContentCard>(`/cards/${encodeURIComponent(id)}/retry`, "POST"),
  document: (id: string) => request<{ text: string; truncated: boolean; filename: string; mime: string }>(`/documents/${encodeURIComponent(id)}/body`),
  documents: () => request<{ documents: { import_id: string; filename: string; state: string; size_bytes?: number; created_at_unix: number }[] }>("/documents"),
  attachmentUrl: (peer: string, message: string) => nodeControlUrl(`/friends/${encodeURIComponent(peer)}/attachments/${encodeURIComponent(message)}`),
};

export function invitationText(invite: FriendInviteResult): string {
  return `Join me on Ryn.\nInstall or open Ryn: https://github.com/yeogirlyun/rynmesh/releases/latest\nOpen Friends, paste the invite below, review my identity and permissions, then choose Add this friend.\nValid until ${new Date(invite.invite.expires_at).toLocaleString()}; one use only. If it expires during installation, ask me for a new invite.\n\n${invite.invite_uri}`;
}

export function extractInvite(text: string): string {
  return text.match(/rynmesh:\/\/[^\s]+/)?.[0] ?? text.trim();
}

export function friendDeliveryExplanation(message: Pick<FriendMessage, "delivery_state" | "error">): string {
  if (message.delivery_state === "expired") return "The delivery window expired without confirmation. Your friend may already have received it. Nothing is resent automatically; review the content before sending a new message or card.";
  if (!["failed", "queued"].includes(message.delivery_state ?? "")) return "";
  const reasons: Record<string, string> = {
    recipient_full: "Your friend's mailbox is full. Wait for space or a direct connection, then retry the same message or card.",
    sender_quota: "Your pending mail to this friend has reached its limit. Wait for them to receive it, then retry the same message or card.",
    rate_limited: "The mailbox is limiting send requests. Wait a little, then retry the same message or card.",
    waiting_for_direct_connection: "This item is waiting for a direct connection. It has not been confirmed as delivered.",
    mailbox_unavailable: "The mailbox could not be reached. Delivery is unconfirmed; check the connection and retry.",
  };
  return reasons[message.error ?? ""] ?? "";
}
