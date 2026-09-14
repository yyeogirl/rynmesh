import { nodeControlUrl } from "./nodeUrl";
import type { ConsumptionRecord } from "./digestClient";

export type SearchSnippet = { text: string; matches: [number, number][]; offset_unit: "unicode_codepoints"; prefix_omitted: boolean; suffix_omitted: boolean };
export type SearchResult = { id: string; title: string; source: string; timestamp: number; kinds: string[]; body_state: string; text_truncated?: boolean;
  targets: { label: string; href: string }[]; snippet: SearchSnippet; title_match: SearchSnippet };
export type SearchStatus = { state: string; error_code: string; indexed_count: number; updated_at: number | null; unavailable_sources?: string[] };
export type SearchPage = { results: SearchResult[]; total: number; next_cursor: string; partial: boolean; indexing_pending?: boolean; unavailable_sources?: string[]; index: SearchStatus };
export type SearchDocument = Omit<SearchResult, "snippet" | "title_match"> & { text: string; reading_record?: ConsumptionRecord; offline_key?: string };
export type SearchRequest = { query: string; kind?: string; source?: string; friend_id?: string; after?: number; before?: number; sort?: string; cursor?: string };
async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(nodeControlUrl(`/search${path}`), { credentials: "include", cache: "no-store", signal,
    method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    const messages: Record<string, string> = {
      search_results_changed: "Results changed. Refresh to start from the first page.",
      search_result_unavailable: "This result was removed or access changed. Return to search and refresh.",
      search_index_version_unsupported: "This index was created by a newer app. Update the app before rebuilding.",
      search_filter_invalid: "Check the dates and filters, then try again.",
      search_index_limit: "The local search index reached its size limit. Your original content is unchanged.",
      search_source_unavailable: "Local content could not be checked. Reconnect to your node and retry.",
      search_index_busy: "The index is already rebuilding. Wait and refresh its status.",
    };
    throw new Error(messages[value.detail] ?? "Search is unavailable. Retry or rebuild the index; your original content is unchanged.");
  }
  return response.json();
}
export const localSearch = {
  query: (body: SearchRequest, signal?: AbortSignal) => request<SearchPage>("/query", body, signal),
  status: () => request<SearchStatus>("/status"),
  rebuild: () => request<SearchStatus>("/rebuild", {}),
  open: (identifier: string) => request<SearchDocument>(`/open?${new URLSearchParams({ identifier })}`),
};
