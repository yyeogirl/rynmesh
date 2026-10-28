import { afterEach, expect, it, vi } from "vitest";
import { ProductExportError, productExport } from "./productExport";

afterEach(() => vi.unstubAllGlobals());
it('posts only scopes with owner credentials and accepts a ZIP response', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('zip', { headers: { 'Content-Type': 'application/zip' } }));
  vi.stubGlobal('fetch', fetch);
  expect((await productExport.download(['conversations'])).size).toBe(3);
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/privacy/export/archive'), expect.objectContaining({
    method: 'POST', credentials: 'include', body: '{"scopes":["conversations"]}',
  }));
});
it('rejects an unexpected response type and does not expose private error text', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(new Response('<html>sign in</html>', { headers: { 'Content-Type': 'text/html' } }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'private-error-secret', scope: 'reading' } }), { status: 503 }));
  vi.stubGlobal('fetch', fetch);
  await expect(productExport.download(['reading'])).rejects.toBeInstanceOf(ProductExportError);
  await expect(productExport.download(['reading'])).rejects.toThrow('The export could not be completed');
});
