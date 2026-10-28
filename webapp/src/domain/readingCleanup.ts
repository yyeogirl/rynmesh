import { nodeControlUrl } from "./nodeUrl";

export type ReadingCleanupStep = "source" | "replica" | "backups" | "search";
export interface ReadingCleanupReview {
  review_token: string; local_items: number; source_entities: number; replica_entities: number; backup_files: number;
}
export interface ReadingCleanupJob {
  id: string; sequence: number; done: ReadingCleanupStep[]; pending: ReadingCleanupStep[];
  cancelled: boolean; local_copies_complete: boolean; remote_confirmed: boolean;
}
export interface ReadingBackupReview { review_token: string; files: number; bytes: number }
export class ReadingCleanupError extends Error {
  constructor(readonly code: string, message: string) { super(message); }
}
const messages: Record<string, string> = {
  reading_privacy_review_changed: "Reading data changed after review. Review the current scope again.",
  reading_cleanup_pending: "An earlier reading cleanup is unfinished. Continue it before starting another.",
  reading_cleanup_backup_changed: "A remaining backup changed. Review its current version before clearing it.",
  reading_cleanup_already_started: "Reading cleanup has already started. Continue the remaining steps; cancellation cannot undo it.",
  reading_cleanup_not_found: "This cleanup is no longer the node’s current operation. Refresh its progress.",
  reading_cleanup_version_unsupported: "Cleanup records need a newer version of Ryn. Existing records are preserved.",
  reading_privacy_version_unsupported: "Reading records need a newer version of Ryn. Existing records are preserved.",
  sync_version_unsupported: "Stored data needs a newer version of Ryn. Cleanup remains unfinished.",
  search_index_busy: "Search is updating. Wait a moment, then retry the same cleanup.",
  consumption_backup_failed: "The node could not preserve the previous reading format. Check storage and retry.",
};
async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(nodeControlUrl(`/privacy/reading${path}`), {
      method, credentials: "include", headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new ReadingCleanupError("unconfirmed", "The node has not confirmed the operation. Refresh progress or retry the original request.");
  }
  if (!response.ok) {
    const value = await response.json().catch(() => ({})) as { detail?: string };
    throw new ReadingCleanupError(value.detail ?? "unavailable", messages[value.detail ?? ""] ?? "Reading cleanup is unfinished. Check storage, refresh progress and retry.");
  }
  return response.json() as Promise<T>;
}
const path = (id: string) => `/job/${encodeURIComponent(id)}`;
export const readingCleanup = {
  preview: () => request<ReadingCleanupReview>("/preview"),
  status: async () => (await request<{ job: ReadingCleanupJob | null }>("/job")).job,
  begin: (review_token: string) => request<ReadingCleanupJob>("/job", "POST", { review_token }),
  resume: (id: string) => request<ReadingCleanupJob>(`${path(id)}/resume`, "POST"),
  cancel: (id: string) => request<ReadingCleanupJob>(`${path(id)}/cancel`, "POST"),
  reviewBackups: (id: string) => request<ReadingBackupReview>(`${path(id)}/backups`),
  approveBackups: (id: string, review_token: string) => request<ReadingCleanupJob>(`${path(id)}/backups`, "POST", { review_token }),
};
