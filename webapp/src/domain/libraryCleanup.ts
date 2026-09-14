import { nodeControlUrl } from "./nodeUrl";

export interface LibraryReview { review_token: string; scope: string | null; documents: number; files: number; bytes: number }
export interface LibraryCleanupJob { id: string; scope: string | null; sequence: number; done: string[]; pending: string[]; local_copies_complete: boolean; remote_confirmed: boolean; removed: number }
export class LibraryCleanupError extends Error {
  constructor(readonly code: string, message: string) { super(message); }
}
const messages: Record<string, string> = {
  library_cleanup_review_changed: "Document copies changed after review. Review them again before clearing.",
  library_cleanup_pending: "Document cleanup is unfinished. Continue it in Settings → Privacy & data. A selected copy can be downloaded again after cleanup completes.",
  library_cleanup_files_changed: "Remaining files changed or include an unrecognized file. Review the remaining managed files before continuing. Unrecognized files are kept.",
  library_cleanup_not_found: "This is no longer the current cleanup. Refresh progress.",
  library_import_version_unsupported: "Saved data needs a newer version of Ryn. It has been kept.",
  library_cleanup_backup_failed: "Could not back up the older control record. Check storage and retry.",
  library_cleanup_limit: "Too many files or operations for this cleanup. Existing data is preserved.",
};
async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  let response: Response;
  try { response = await fetch(nodeControlUrl(`/privacy/documents${path}`), { method, credentials: "include", headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }); }
  catch { throw new LibraryCleanupError("unconfirmed", "Document cleanup was not confirmed. Refresh progress or retry the original request."); }
  if (!response.ok) {
    const value = await response.json().catch(() => ({})) as { detail?: string };
    throw new LibraryCleanupError(value.detail ?? "unavailable", messages[value.detail ?? ""] ?? "Document cleanup is unfinished. Check storage, refresh progress and retry.");
  }
  return response.json() as Promise<T>;
}
export const libraryCleanup = {
  preview: (scope: string | null = null) => request<LibraryReview>("/preview", "POST", { scope }),
  status: async () => (await request<{ job: LibraryCleanupJob | null }>("/job")).job,
  begin: (review: Pick<LibraryReview, 'review_token' | 'scope'>) => request<LibraryCleanupJob>("/job", "POST", { scope: review.scope, review_token: review.review_token }),
  resume: (id: string) => request<LibraryCleanupJob>(`/job/${encodeURIComponent(id)}/resume`, "POST"),
  reviewFiles: (id: string) => request<{ review_token: string; files: number; bytes: number }>(`/job/${encodeURIComponent(id)}/files`),
  approveFiles: (id: string, review_token: string) => request<LibraryCleanupJob>(`/job/${encodeURIComponent(id)}/files`, "POST", { review_token }),
};
export const libraryCleanupScope = "Removes reviewed document files, extracted text and their managed repair or interrupted-write copies. Messages, cards and reading bookmarks remain. Offline downloads, search caches, browser copies and other people's copies are separate. Selected documents stop opening and sharing when cleanup starts. In-flight downloads cannot restore them; download a selected copy again after cleanup completes.";
export const libraryReviewCounts = (value: LibraryReview) => `${value.documents} document copies, ${value.files} files, ${(value.bytes / 1024).toFixed(1)} KiB.`;
