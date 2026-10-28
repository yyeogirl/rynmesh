import { webcrypto } from "node:crypto";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import { offlineApi } from "../domain/offlineReading";
import { readingTextVersion } from "../domain/readingHistory";
import ContentViewer from "./ContentViewer";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function setup(kind: "same" | "unknown" | "different" | "conflict" = "same") {
  vi.stubGlobal("crypto", webcrypto);
  const client = { ...makeFixtureNodeClient(), mode: "live" as const };
  const item = (await client.listContent()).find((row) => row.content_kind === "document")!;
  const text = "The complete local body with a saved position.";
  const version = await readingTextVersion([text]);
  const record = { item_id: item.content_id, progress: .4, sync_revisions: { reading: "revision-one" },
    sync_conflicts: { reading: kind === "conflict" }, content_version: kind === "unknown" ? "" : kind === "different" ? "other-text" : version } as ConsumptionRecord;
  vi.spyOn(offlineApi, "resolve").mockResolvedValue(null);
  vi.spyOn(client, "getContentBody").mockResolvedValue({ ok: true, content_id: item.content_id, text, content_type: "text/plain", size: "100", truncated: false });
  const history = vi.spyOn(digestApi, "listConsumption").mockResolvedValue([record]);
  const write = vi.spyOn(client, "recordContentConsumption").mockResolvedValue({ ...record, sync_revisions: { reading: "revision-two" } });
  const close = vi.fn();
  const view = render(<MemoryRouter><ContentViewer item={item} client={client} onClose={close} /></MemoryRouter>);
  const stage = view.container.querySelector(".content-viewer-stage") as HTMLElement;
  Object.defineProperties(stage, { scrollHeight: { configurable: true, value: 1500 }, clientHeight: { configurable: true, value: 500 } });
  await screen.findByText(text);
  return { ...view, client, item, stage, version, write, close, history, record, user: userEvent.setup() };
}

it("restores matching text and guards sequential edits with each acknowledged revision", async () => {
  const result = await setup();
  await waitFor(() => expect(result.stage.scrollTop).toBe(400));
  result.stage.scrollTop = 200; fireEvent.scroll(result.stage);
  await waitFor(() => expect(result.write).toHaveBeenCalledWith(result.item, "progress", .2,
    { content_version: result.version, expected_sync_revision: "revision-one" }));
  result.stage.scrollTop = 600; fireEvent.scroll(result.stage);
  await waitFor(() => expect(result.write).toHaveBeenLastCalledWith(result.item, "progress", .6,
    { content_version: result.version, expected_sync_revision: "revision-two" }));
});

it("opening and closing an unchanged synced position does not submit a new position", async () => {
  const result = await setup();
  await waitFor(() => expect(result.stage.scrollTop).toBe(400));
  await result.user.click(screen.getByRole("button", { name: "Close content viewer" }));
  expect(result.close).toHaveBeenCalledOnce(); expect(result.write).not.toHaveBeenCalled();
});

it.each(["unknown", "different"] as const)("reviews %s text before using an approximate saved percentage", async (kind) => {
  const result = await setup(kind);
  await screen.findByRole("button", { name: "Use saved percentage" });
  expect(result.stage.scrollTop).toBe(0);
  result.stage.scrollTop = 100; fireEvent.scroll(result.stage);
  expect(result.write).not.toHaveBeenCalled();
  await result.user.click(screen.getByRole("button", { name: "Use saved percentage" }));
  await waitFor(() => expect(result.stage.scrollTop).toBe(400));
});

it("can explicitly start earlier while preserving the reviewed revision", async () => {
  const result = await setup("different");
  await result.user.click(await screen.findByRole("button", { name: "Start at the beginning" }));
  await new Promise((resolve) => requestAnimationFrame(resolve));
  await result.user.click(screen.getByRole("button", { name: "Close content viewer" }));
  expect(result.write).toHaveBeenCalledWith(result.item, "progress", 0,
    { content_version: result.version, expected_sync_revision: "revision-one" });
});

it("keeps conflicting choices intact while reading and closing", async () => {
  const result = await setup("conflict");
  expect(await screen.findByRole("link", { name: "Review reading choices" })).toHaveAttribute("href", "/devices#reading-sync-conflicts");
  expect(screen.queryByRole("button", { name: "Use saved percentage" })).not.toBeInTheDocument();
  result.stage.scrollTop = 800; fireEvent.scroll(result.stage);
  await result.user.click(screen.getByRole("button", { name: "Close content viewer" }));
  expect(result.write).not.toHaveBeenCalled(); expect(result.close).toHaveBeenCalledOnce();
});

it("keeps a rejected stale save visible and reloads current source state on request", async () => {
  const result = await setup();
  await waitFor(() => expect(result.stage.scrollTop).toBe(400));
  result.write.mockRejectedValue(new Error("sync_revision_conflict"));
  result.stage.scrollTop = 700; fireEvent.scroll(result.stage);
  expect(await screen.findByRole("alert")).toHaveTextContent("another device changed it");
  result.history.mockResolvedValue([{ ...result.record, progress: .2, sync_revisions: { reading: "remote-new" } }]);
  await result.user.click(screen.getByRole("button", { name: "Reload saved position" }));
  await waitFor(() => expect(result.stage.scrollTop).toBe(200));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("reloads after queued source writes settle instead of reusing their old revision", async () => {
  const result = await setup();
  await waitFor(() => expect(result.stage.scrollTop).toBe(400));
  result.write.mockRejectedValueOnce(new Error("temporary failure"));
  result.stage.scrollTop = 700; fireEvent.scroll(result.stage);
  await screen.findByRole("alert");
  let finish!: (value: ConsumptionRecord) => void;
  result.write.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  await result.user.click(screen.getByRole("button", { name: "Retry saving position" }));
  await waitFor(() => expect(result.write).toHaveBeenCalledTimes(2));
  await result.user.click(screen.getByRole("button", { name: "Reload saved position" }));
  // Body loading can finish, but the history snapshot waits for the outstanding save.
  await screen.findByText("Loading the article through your Ryn…");
  expect(result.history).toHaveBeenCalledTimes(1);
  const updated = { ...result.record, progress: .7, sync_revisions: { reading: "after-save" } };
  result.history.mockResolvedValue([updated]); finish(updated);
  await screen.findByText("The complete local body with a saved position.");
  await waitFor(() => expect(result.stage.scrollTop).toBe(700));
  result.stage.scrollTop = 200; fireEvent.scroll(result.stage);
  await waitFor(() => expect(result.write).toHaveBeenLastCalledWith(result.item, "progress", .2,
    { content_version: result.version, expected_sync_revision: "after-save" }));
});
