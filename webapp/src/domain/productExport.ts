import { nodeControlUrl } from "./nodeUrl";

export interface ExportScope { id: string; label: string }
export interface ExportOptions { scopes: ExportScope[]; excluded: string[] }
export class ProductExportError extends Error {
  constructor(readonly code: string, readonly scope?: string) {
    super(code === "privacy_export_busy" ? "Another export is still being prepared or downloaded. Wait for it to finish, then retry."
      : code === "privacy_export_source_changed" ? "Data changed while it was being read. No archive was returned; retry the selected scope."
        : code === "privacy_export_capacity_exhausted" ? "The selected export is too large. Select fewer scopes and retry."
          : "The export could not be completed. Retry, or select other scopes to export them separately.");
  }
}
async function checked(response: Response) {
  if (!response.ok) {
    const data = await response.json().catch(() => ({})) as { detail?: { code?: string; scope?: string } };
    throw new ProductExportError(data.detail?.code ?? "privacy_export_unavailable", data.detail?.scope);
  }
  return response;
}
export const productExport = {
  options: async (signal?: AbortSignal): Promise<ExportOptions> => (await checked(await fetch(nodeControlUrl('/privacy/export/scopes'),
    { credentials: 'include', signal }))).json() as Promise<ExportOptions>,
  download: async (scopes: string[], signal?: AbortSignal): Promise<Blob> => {
    const response = await checked(await fetch(nodeControlUrl('/privacy/export/archive'), {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scopes }), signal,
    }));
    if (!response.headers.get('Content-Type')?.startsWith('application/zip')) throw new ProductExportError('privacy_export_unavailable');
    return response.blob();
  },
  save: (blob: Blob) => {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `ryn-product-data-${new Date().toISOString().slice(0, 10)}.zip`;
    anchor.click();
    // Allow the browser to consume the URL before releasing its memory.
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  },
};
