import { nodeControlUrl } from "./nodeUrl";

export type OfflineRecord = { key: string; item_id: string; reference: { item_id: string; title: string; source: string; url: string };
  state: string; error_code: string; verified_bytes: number;
  current: { job_id: string; downloaded_at: number; partial: boolean; size_bytes: number } | null };
export type OfflineStatus = { records: OfflineRecord[]; used_bytes: number; download_bytes: number;
  cleanup?: { review_token: string; item_id: string | null; sequence: number; done: boolean; copies: number; bytes: number } | null;
  limits: { item_bytes: number; total_bytes: number; image_bytes: number; image_count: number } };
export type OfflineBody = { item_id: string; title: string; source: string; url: string; text: string; truncated: boolean;
  images_omitted: boolean; source_mode: string; job_id: string; downloaded_at: number; partial: boolean;
  images: { index: number; alt: string; state: string; error_code: string; mime?: string }[] };
export type ClearReview = { review_token: string; copies: number; bytes: number; pending: number };
export const offlineActive = (state: string) => ["queued", "downloading", "verifying", "cancel_requested"].includes(state);
export const offlineLabels: Record<string, string> = { queued: "Queued", downloading: "Downloading", verifying: "Verifying",
  ready: "Body available offline", partial: "Body available; some resources missing", failed: "Download failed",
  cancel_requested: "Cancellation requested", cancelled: "Cancelled", cleared: "Cleared" };
export const bytesLabel = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1048576).toFixed(1)} MiB`;
const errors: Record<string, string> = {
  offline_not_downloaded: "The body has not been downloaded. Connect to the source and download it first.",
  offline_source_unreachable: "The source could not be reached. Reconnect and retry.",
  offline_source_timeout: "The source took too long. Retry when it is available.",
  offline_storage_limit: "The download storage limit was reached. Clear some downloads and retry.",
  offline_disk_full: "This device is short of space. Free space and retry.",
  offline_item_limit: "This item exceeds the download size limit. Other downloads are kept.",
  offline_body_too_large: "This article exceeds the body size limit.",
  offline_resource_too_large: "This resource exceeds the size limit.",
  offline_source_access_required: "The source requires access or sign-in. This download cannot bypass it.",
  offline_source_address_blocked: "This source address is not supported for web downloads.",
  offline_source_format_unsupported: "This source format is not supported for offline reading.",
  offline_media_unsupported: "Full video, audio and standalone image downloads are not supported here.",
  offline_no_readable_body: "No readable body was found. An empty page was not saved.",
  offline_item_unavailable: "Open or save this item first, then retry the download.",
  offline_clear_review_changed: "Downloads changed after your review. Review the current count and space before clearing.",
  offline_cleanup_pending: "An earlier file cleanup is unfinished. Continue it before clearing other downloads.",
  offline_cleanup_copy_changed: "A remaining file changed after review. Review its current version before clearing it.",
  offline_cleanup_version_unsupported: "Cleanup records need a newer app version. Existing files and progress have been kept.",
  offline_cleanup_unreadable: "Cleanup progress could not be read. Existing files have been kept; check your local node and retry.",
  offline_cleanup_copy_too_large: "A managed copy exceeds the supported cleanup size. No new cleanup has started.",
  offline_version_unsupported: "This download needs a newer app version. Existing files have been kept.",
  offline_copy_changed: "This copy was updated or cleared. Close and reopen it to read the current version.",
  offline_verification_failed: "The saved copy failed verification. Reconnect and download a new version.",
  offline_source_verification_failed: "The source copy failed verification. Open its original saved document to recover it.",
  offline_resuming_verified_checkpoints: "Resuming from verified saved resources; unfinished resources download again.",
};
export const offlineError = (code: string) => errors[code] ?? "This operation could not be confirmed. Refresh the downloads and retry.";
export class OfflineOperationError extends Error {
  constructor(readonly code: string) { super(offlineError(code)); }
}
async function request<T>(action = "", body?: unknown): Promise<T> {
  const response = await fetch(nodeControlUrl(`/offline-reading${action ? `/${action}` : ""}`), {
    method: body === undefined ? "GET" : "POST", credentials: "include",
    headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new OfflineOperationError(value.detail);
  }
  return response.json();
}
export const offlineApi = {
  resolve: (item_id: string) => request<{ key: string; body: OfflineBody } | null>("resolve", { item_id }),
  status: () => request<OfflineStatus>(),
  download: (item_id: string, update = false) => request<OfflineRecord>("download", { item_id, update }),
  retry: (item_id: string) => request<OfflineRecord>("retry", { item_id }),
  cancel: (item_id: string) => request<OfflineRecord>("cancel", { item_id }),
  body: (item_id: string) => request<OfflineBody>("body", { item_id }),
  review: (item_id?: string) => request<ClearReview>("clear-preview", { item_id }),
  clear: (review_token: string, item_id?: string) => request<ClearReview & { freed_bytes: number }>("clear", { review_token, item_id }),
  reviewRemaining: () => request<{ review_token: string; files: number; bytes: number }>("clear-remaining-preview", {}),
  clearRemaining: (review_token: string) => request<ClearReview & { freed_bytes: number }>("clear-remaining", { review_token }),
  image: async (key: string, job: string, index: number, signal: AbortSignal) => {
    const response = await fetch(nodeControlUrl(`/offline-reading/copies/${encodeURIComponent(key)}/${encodeURIComponent(job)}/images/${index}`),
      { credentials: "include", signal });
    if (!response.ok) throw new Error("Image unavailable in this saved version.");
    return response.blob();
  },
};
