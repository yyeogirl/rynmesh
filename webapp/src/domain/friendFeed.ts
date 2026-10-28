import { nodeControlUrl } from "./nodeUrl";
import type { FriendContentCard, FriendRecord } from "./friendTypes";

export type FeedAudience = { mode: "selected" | "all_friends"; relationship_ids: string[] };
export type FeedCard = Omit<FriendContentCard["card"], "library_id"> & { library_id?: string };
export type FeedEntry = { id: string; revision: number; published_at: number; updated_at: number; card: FeedCard; read?: boolean };
export type FeedPublication = { id: string; revision: number; stopped: boolean; draft: { card: FeedCard; audience: FeedAudience } | null;
  published: (FeedEntry & { audience: FeedAudience }) | null; current_audience?: FriendRecord[] };
export type FeedSubscription = { relationship_id: string; peer_id: string; enabled: boolean; revision: number };
export type FeedTimeline = { relationship_id: string; peer_id: string; node_name: string; subscription_revision: number;
  checked_at: number | null; next_cursor: string; error_code: string; rows: FeedEntry[] };
export type FeedSnapshot = { subscriptions: FeedSubscription[]; timeline: FeedTimeline[] };

const errors: Record<string, string> = {
  feed_revision_conflict: "This draft or subscription changed elsewhere. Refresh and review the latest version.",
  feed_operation_conflict: "This attempt contains different changes. Refresh and review before retrying.",
  feed_friend_inactive: "This friendship is no longer active. Refresh your friends list.",
  feed_publication_unavailable: "This publication is no longer available to you. Refresh the updates.",
  feed_publication_erased: "This publication was cleared. Choose Start another draft to publish a new item.",
  feed_publication_changed: "This publication changed. Refresh and review the current version.",
  feed_results_changed: "Updates changed during pagination. Refresh this friend to continue.",
  feed_friend_unreachable: "Your friend could not be reached. Previously checked entries remain available; retry later.",
  feed_content_changed: "The reviewed copy is missing or changed. Open the source and prepare a new draft.",
  feed_future_friends_confirmation_required: "Confirm that all current and future friends may access this publication.",
  feed_version_unsupported: "This feed needs a newer app version. Existing data has been kept.",
  feed_capacity_exhausted: "The local feed has reached its storage limit. The operation was not saved.",
};

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(nodeControlUrl(`/friend-feed${path}`), { method, credentials: "include",
    headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new Error(errors[value.detail] ?? "This operation could not be confirmed. Your saved draft or copy remains available. Refresh and retry.");
  }
  return response.json();
}
const segment = encodeURIComponent;
export const feedApi = {
  snapshot: () => request<FeedSnapshot>(""),
  publications: () => request<{ publications: FeedPublication[] }>("/publications"),
  draft: (id: string, value: { reference: { item_id: string }; audience: FeedAudience; expected_revision: number; operation_id: string }) =>
    request<FeedPublication>(`/publications/${segment(id)}/draft`, "POST", value),
  publish: (id: string, value: { expected_revision: number; operation_id: string; confirm_all_friends: boolean }) =>
    request<FeedPublication>(`/publications/${segment(id)}/publish`, "POST", value),
  stop: (id: string, value: { expected_revision: number; operation_id: string }) =>
    request<FeedPublication>(`/publications/${segment(id)}/stop`, "POST", value),
  subscribe: (rid: string, enabled: boolean, expected_revision: number) => request<FeedSubscription>(`/subscriptions/${segment(rid)}`, "PUT", { enabled, expected_revision }),
  refresh: (rid: string, cursor = "") => request(`/subscriptions/${segment(rid)}/refresh`, "POST", { cursor }),
  fetch: (rid: string, id: string, expected_revision: number) => request<{ library_id: string }>(`/subscriptions/${segment(rid)}/${segment(id)}/fetch`, "POST", { expected_revision }),
  read: (rid: string, id: string, expected_revision: number) => request(`/subscriptions/${segment(rid)}/${segment(id)}/read`, "POST", { expected_revision }),
};
