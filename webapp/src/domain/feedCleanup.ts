import { nodeControlUrl } from "./nodeUrl";

export interface FeedCleanupReview {
  review_token: string; publications: number; active_publications: number; subscriptions: number; received_updates: number; backup_files: number;
}
export interface FeedCleanupJob {
  id: string; sequence: number; done: string[]; pending: string[]; cancelled: boolean; local_copies_complete: boolean; remote_confirmed: boolean;
}
interface BackupReview { review_token: string; files: number; bytes: number }
export class FeedCleanupError extends Error {
  constructor(readonly code: string, message: string) { super(message); }
}
const messages: Record<string, string> = {
  feed_cleanup_review_changed: "Your friend updates changed after review. Review the current data again.",
  feed_cleanup_pending: "An earlier friend update cleanup is unfinished. Continue its remaining steps.",
  feed_cleanup_backup_changed: "A remaining backup changed. Review its current version before clearing it.",
  feed_cleanup_not_found: "This is no longer the current cleanup. Refresh its progress.",
  feed_version_unsupported: "Saved friend updates need a newer version of Ryn. The data has been kept.",
  feed_cleanup_version_unsupported: "Cleanup records need a newer version of Ryn. The data has been kept.",
  feed_backup_failed: "The previous feed format could not be backed up. Check storage and retry.",
  feed_cleanup_limit: "Cleanup has reached its identity or sequence limit. Existing records are preserved; update Ryn before continuing.",
  feed_cleanup_backup_limit: "There are too many interrupted-write copies to review in this operation.",
};
async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(nodeControlUrl(`/privacy/friend-feed${path}`), { method, credentials: "include",
      headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  } catch {
    throw new FeedCleanupError("unconfirmed", "The node did not confirm cleanup. Refresh progress or retry the original request.");
  }
  if (!response.ok) {
    const value = await response.json().catch(() => ({})) as { detail?: string };
    throw new FeedCleanupError(value.detail ?? "unavailable", messages[value.detail ?? ""] ?? "Friend update cleanup is unfinished. Check storage, refresh progress and retry.");
  }
  return response.json() as Promise<T>;
}
const path = (id: string) => `/job/${encodeURIComponent(id)}`;
export const feedCleanup = {
  preview: () => request<FeedCleanupReview>("/preview"),
  status: async () => (await request<{ job: FeedCleanupJob | null }>("/job")).job,
  begin: (review_token: string) => request<FeedCleanupJob>("/job", "POST", { review_token }),
  resume: (id: string) => request<FeedCleanupJob>(`${path(id)}/resume`, "POST"),
  reviewBackups: (id: string) => request<BackupReview>(`${path(id)}/backups`),
  approveBackups: (id: string, review_token: string) => request<FeedCleanupJob>(`${path(id)}/backups`, "POST", { review_token }),
};
