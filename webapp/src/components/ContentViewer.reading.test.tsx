import { MemoryRouter } from "react-router-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import ContentViewer from "./ContentViewer";
import { offlineApi, type OfflineBody } from "../domain/offlineReading";
import { friendsApi } from "../domain/friendsClient";

afterEach(() => vi.restoreAllMocks());

it("keeps bookmark state until a save succeeds and can remove it from an ordinary reading view", async () => {
  vi.spyOn(offlineApi, "resolve").mockResolvedValue(null);
  const client = { ...makeFixtureNodeClient(), mode: "live" as const };
  const item = (await client.listContent()).find((row) => row.content_kind === "document")!;
  vi.spyOn(client, "getContentBody").mockResolvedValue({ ok: true, content_id: item.content_id,
    content_type: "text/plain", size: "20", truncated: false, text: "Saved-list article body." });
  vi.spyOn(digestApi, "listConsumption").mockResolvedValue([]);
  const write = vi.spyOn(client, "recordContentConsumption").mockRejectedValueOnce(new Error("disk full"))
    .mockResolvedValueOnce({ bookmarked: true } as ConsumptionRecord)
    .mockResolvedValueOnce({ bookmarked: false } as ConsumptionRecord);
  render(<MemoryRouter><ContentViewer item={item} client={client} onClose={vi.fn()} /></MemoryRouter>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Save for later" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("saved choice could not be confirmed");
  expect(screen.queryByRole("button", { name: "Remove from saved" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save for later" }));
  await user.click(await screen.findByRole("button", { name: "Remove from saved" }));
  expect(await screen.findByRole("button", { name: "Save for later" })).toBeEnabled();
  expect(write.mock.calls.map((call) => call[1])).toEqual(["bookmark", "bookmark", "unbookmark"]);
});

it("restores position, reports truncated content and retries a failed position save before closing", async () => {
  vi.spyOn(offlineApi, 'resolve').mockResolvedValue(null);
  const client = { ...makeFixtureNodeClient(), mode: "live" as const };
  const item = (await client.listContent()).find((row) => row.content_kind === "document")!;
  vi.spyOn(client, "getContentBody").mockResolvedValue({ ok: true, content_id: item.content_id,
    content_type: "text/plain", size: "20", truncated: true, text: "A real saved article." });
  vi.spyOn(digestApi, "listConsumption").mockResolvedValue([
    { item_id: item.content_id, progress: 0.4 } as ConsumptionRecord,
  ]);
  const write = vi.spyOn(client, "recordContentConsumption").mockResolvedValue(undefined);
  const close = vi.fn();
  const { container } = render(<MemoryRouter><ContentViewer item={item} client={client} onClose={close} /></MemoryRouter>);
  const stage = container.querySelector(".content-viewer-stage") as HTMLElement;
  Object.defineProperties(stage, { scrollHeight: { configurable: true, value: 1500 }, clientHeight: { configurable: true, value: 500 } });
  await screen.findByText("A real saved article.");
  await waitFor(() => expect(stage.scrollTop).toBe(400));
  expect(screen.getByText(/shortened preview/)).toBeInTheDocument();
  stage.scrollTop = 700;
  fireEvent.scroll(stage);
  await waitFor(() => expect(write).toHaveBeenCalledWith(item, "progress", 0.7));
  write.mockRejectedValueOnce(new Error("disk full"));
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Close content viewer" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be saved");
  expect(close).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Close content viewer" }));
  expect(close).toHaveBeenCalledTimes(1);
});

it("prefers a verified offline copy in the ordinary reading entry", async () => {
  const client = { ...makeFixtureNodeClient(), mode: "live" as const };
  const item = { ...(await client.listContent()).find((row) => row.content_kind === "document")!, external_url: "https://example.test/online" };
  vi.spyOn(offlineApi, 'resolve').mockResolvedValue({ key: "key", body: { item_id: item.content_id, text: "Local-first reading body.",
    source: "Saved notebook", downloaded_at: 1800000000, job_id: "a".repeat(32), images: [], truncated: false } as unknown as OfflineBody });
  const external = vi.spyOn(digestApi, 'readArticle').mockRejectedValue(new Error("Source is offline"));
  const native = vi.spyOn(client, 'getContentBody');
  vi.spyOn(digestApi, 'listConsumption').mockResolvedValue([]);
  render(<MemoryRouter><ContentViewer client={client} item={item} onClose={vi.fn()} /></MemoryRouter>);
  expect(await screen.findByText("Local-first reading body.")).toBeInTheDocument();
  expect(external).not.toHaveBeenCalled(); expect(native).not.toHaveBeenCalled();
  expect(screen.getByText(/Offline copy.*Saved notebook/)).toBeInTheDocument();
});

it("reports that an ordinary bookmark is not downloaded when the source also fails", async () => {
  const client = { ...makeFixtureNodeClient(), mode: "live" as const };
  const item = { ...(await client.listContent()).find((row) => row.content_kind === "document")!, external_url: "https://example.test/online" };
  vi.spyOn(offlineApi, 'resolve').mockResolvedValue(null);
  vi.spyOn(digestApi, 'readArticle').mockRejectedValue(new Error("Source is offline"));
  render(<MemoryRouter><ContentViewer client={client} item={item} onClose={vi.fn()} /></MemoryRouter>);
  expect(await screen.findByRole("alert")).toHaveTextContent("has not been downloaded for offline reading");
  expect(screen.queryByText(/Offline copy ·/)).not.toBeInTheDocument();
});

it("offers an authorized download entry for a missing private reference without claiming a saved copy", async () => {
  const client = { ...makeFixtureNodeClient(), mode: "live" as const };
  const item = { ...(await client.listContent()).find((row) => row.content_kind === "document")!, content_id: "import:missing-copy" };
  vi.spyOn(offlineApi, "resolve").mockResolvedValue(null);
  vi.spyOn(friendsApi, "document").mockRejectedValue(new Error("Private copy is unavailable"));
  const opened = vi.fn();
  render(<MemoryRouter><ContentViewer client={client} item={item} onRead={opened} onClose={vi.fn()} /></MemoryRouter>);
  expect(await screen.findByRole("alert")).toHaveTextContent("Private copy is unavailable");
  expect(screen.getByRole("link", { name: "Open Friends to download a copy you can access" })).toHaveAttribute("href", "/friends");
  expect(screen.getByText("private document reference")).toBeInTheDocument();
  expect(screen.queryByText("private saved copy")).not.toBeInTheDocument(); expect(opened).not.toHaveBeenCalled();
});
