import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { digestApi, type ConsumptionRecord, type DigestItem } from "../domain/digestClient";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { offlineApi, type OfflineBody } from "../domain/offlineReading";
import ContentViewer from "./ContentViewer";
import DigestViewer from "./DigestViewer";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function setup(kind: "content" | "digest") {
  vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL: () => "blob:saved-chart", revokeObjectURL: vi.fn() }));
  vi.spyOn(offlineApi, "image").mockResolvedValue(new Blob(["chart"], { type: "image/png" }));
  const client = { ...makeFixtureNodeClient(), mode: "live" as const };
  const item = (await client.listContent()).find((row) => row.content_kind === "document")!;
  const body = { item_id: item.content_id, text: "Body before the delayed image.", source: "Saved journal", downloaded_at: 1800000000,
    job_id: "a".repeat(32), images: [{ index: 0, state: "verified", alt: "Saved chart" }], truncated: false } as OfflineBody;
  vi.spyOn(offlineApi, "resolve").mockResolvedValue({ key: "key", body });
  vi.spyOn(digestApi, "listConsumption").mockResolvedValue([{ item_id: item.content_id, progress: 0.4 } as ConsumptionRecord]);
  const write = vi.fn().mockResolvedValue(undefined), close = vi.fn();
  vi.spyOn(client, "recordContentConsumption").mockImplementation(write);
  const digestItem = { item_id: item.content_id, title: item.title, link: "https://example.test/article", content_kind: "document" } as DigestItem;
  const view = render(<MemoryRouter>{kind === "content"
    ? <ContentViewer client={client} item={item} onClose={close} />
    : <DigestViewer items={[digestItem]} index={0} onIndexChange={vi.fn()} onClose={close} onFeedback={vi.fn()}
        onSteer={vi.fn()} bookmarked={true} onBookmark={vi.fn()} onProgress={write} initialProgress={0.4} />}</MemoryRouter>);
  const stage = view.container.querySelector(kind === "content" ? ".content-viewer-stage" : ".viewer-body") as HTMLElement;
  let height = 1000;
  Object.defineProperties(stage, { scrollHeight: { get: () => height + 500 }, clientHeight: { value: 500 } });
  vi.spyOn(stage, "scrollTo").mockImplementation((options) => { stage.scrollTop = (options as ScrollToOptions).top ?? 0; });
  await screen.findByText(body.text);
  const image = await screen.findByRole("img", { name: "Saved chart" });
  return { stage, image, write, close, view, setHeight: (next: number) => { height = next; }, closeLabel: kind === "content" ? "Close content viewer" : "Close" };
}

describe.each(["content", "digest"] as const)("%s reader offline image layout", (kind) => {
  it.each(["load", "error"] as const)("restores against the final height after image %s", async (event) => {
    const result = await setup(kind);
    expect(result.stage.scrollTop).toBe(0);
    fireEvent.scroll(result.stage);
    expect(result.write).not.toHaveBeenCalled();
    result.setHeight(2000);
    fireEvent[event](result.image);
    await waitFor(() => expect(result.stage.scrollTop).toBe(800));
    fireEvent.click(screen.getByRole("button", { name: result.closeLabel }));
    await waitFor(() => expect(result.close).toHaveBeenCalledOnce());
    expect(result.write.mock.calls[0].at(-1)).toBe(0.4);
  });

  it("keeps the owner's scroll instead of restoring over it when the image completes", async () => {
    const result = await setup(kind);
    fireEvent.wheel(result.stage);
    result.stage.scrollTop = 240;
    fireEvent.scroll(result.stage);
    result.setHeight(2000);
    fireEvent.load(result.image);
    await waitFor(() => expect(screen.queryByText(/Loading saved images before/)).not.toBeInTheDocument());
    // The restoration frame arms saving without changing a deliberate scroll.
    await new Promise((resolve) => window.requestAnimationFrame(resolve));
    expect(result.stage.scrollTop).toBe(240);
    fireEvent.click(screen.getByRole("button", { name: result.closeLabel }));
    await waitFor(() => expect(result.close).toHaveBeenCalledOnce());
    expect(result.write.mock.calls[0].at(-1)).toBe(0.12);
  });

  it("does not overwrite the saved position when closed before images settle", async () => {
    const result = await setup(kind);
    fireEvent.click(screen.getByRole("button", { name: result.closeLabel }));
    await waitFor(() => expect(result.close).toHaveBeenCalledOnce());
    expect(result.write).not.toHaveBeenCalled();
    result.view.unmount();
  });
});
