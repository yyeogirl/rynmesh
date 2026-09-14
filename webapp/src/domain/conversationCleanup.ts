import { nodeControlUrl } from "./nodeUrl";

export type CleanupStep = "source" | "replica" | "backups" | "search" | "orders";
export interface CleanupReview {
  review_token: string; conversations: number; recovery_items: number; identities: number;
  has_unassigned_draft: boolean; active_tasks: number; backup_files: number; backup_bytes: number; order_results: number;
}
export interface CleanupJob {
  id: string; sequence?: number; done: CleanupStep[]; pending: CleanupStep[]; cancelled: boolean;
  local_copies_complete: boolean; browser_cleanup_required: boolean; remote_confirmed: boolean; identities: number;
}
export interface BackupReview { review_token: string; files: number; bytes: number }
export class CleanupError extends Error {
  constructor(readonly code: string, message: string) { super(message); }
}
const messages: Record<string, string> = {
  ask_privacy_review_changed: "Your conversations changed after review. Review the current scope before clearing it.",
  ask_privacy_tasks_active: "An original AI task is still being checked. Wait for its outcome before clearing conversations.",
  ask_cleanup_orders_active: "An original AI task is still finishing. Its saved result has not been cleared; retry after it finishes.",
  ask_cleanup_pending: "Finish or cancel the existing cleanup before starting another review.",
  ask_cleanup_already_started: "Conversation erasure already started and cannot be undone. Continue the remaining cleanup steps.",
  ask_cleanup_backup_changed: "A remaining backup changed. Review that backup again before clearing it.",
  ask_cleanup_not_found: "The node has no record of this cleanup. Refresh the list and review your current data.",
  ask_cleanup_limit: "This node has reached its cleanup sequence limit. Existing records are preserved; update Ryn before starting another cleanup.",
  ask_cleanup_backup_failed: "The node could not preserve its previous cleanup records for migration. Check available storage and retry the same request.",
  ask_cleanup_backup_limit: "There are too many backup files to review in one operation. No new cleanup was started.",
  ask_cleanup_version_unsupported: "Cleanup records need a newer version of Ryn. They have been kept unchanged.",
  ask_history_version_unsupported: "Saved conversations need a newer version of Ryn. They have been kept unchanged.",
  search_index_busy: "Search is updating its index. Wait a moment, then retry the same cleanup.",
  search_index_version_unsupported: "The search index needs a newer version of Ryn. Cleanup remains unfinished.",
};
async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(nodeControlUrl(`/privacy/conversations${path}`), {
      method, credentials: "include", headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new CleanupError("unconfirmed", "The node did not confirm this operation. Refresh its progress or retry the same request; it may already have started.");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({})) as { detail?: string };
    throw new CleanupError(data.detail ?? "unavailable", messages[data.detail ?? ""] ?? "Cleanup is unavailable. Your last confirmed progress is retained; refresh and retry.");
  }
  return response.json() as Promise<T>;
}
const path = (id: string) => `/jobs/${encodeURIComponent(id)}`;
export const conversationCleanup = {
  preview: () => request<CleanupReview>("/preview"),
  jobs: async () => (await request<{ jobs: CleanupJob[] }>("/jobs")).jobs,
  begin: (review_token: string) => request<CleanupJob>("/jobs", "POST", { review_token }),
  resume: (id: string) => request<CleanupJob>(`${path(id)}/resume`, "POST"),
  cancel: (id: string) => request<CleanupJob>(`${path(id)}/cancel`, "POST"),
  reviewBackups: (id: string) => request<BackupReview>(`${path(id)}/backups`),
  approveBackups: (id: string, review_token: string) => request<CleanupJob>(`${path(id)}/backups`, "POST", { review_token }),
};
