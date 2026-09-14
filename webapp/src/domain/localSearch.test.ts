import { afterEach, expect, it, vi } from "vitest";
import { localSearch } from "./localSearch";

afterEach(() => vi.unstubAllGlobals());
it("sends private keywords in a local POST body and excludes them from the URL", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ results: [] }) });
  vi.stubGlobal("fetch", fetch);
  const query = "私有搜索关键词";
  await localSearch.query({ query });
  const [url, options] = fetch.mock.calls[0];
  expect(url).toMatch(/\/api\/local\/search\/query$/);
  expect(url).not.toContain(query);
  expect(options.method).toBe("POST");
  expect(JSON.parse(options.body)).toEqual({ query });
});

it("rechecks private opened content without reading or writing the HTTP cache", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ text: "current content" }) });
  vi.stubGlobal("fetch", fetch);
  await localSearch.open("content:private-id");
  expect(fetch.mock.calls[0][1]).toMatchObject({ cache: "no-store", credentials: "include" });
});
