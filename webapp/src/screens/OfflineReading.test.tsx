import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { offlineApi, type OfflineBody, type OfflineRecord, type OfflineStatus } from "../domain/offlineReading";
import type { ConfirmRequest } from "../domain/types";
import OfflineReading from "./OfflineReading";

const context = vi.hoisted(() => ({ value: {} as object }));
vi.mock("../appContext", () => ({ useAppContext: () => context.value }));
const item = { item_id: "article", title: "Offline article", source_title: "Journal", link: "https://example.test/article", content_kind: "document", summary: "", tags: [] as string[] };
const saved = { item_id: "article", item, progress: 0.4, bookmarked: true, open_count: 1 } as ConsumptionRecord;
const record: OfflineRecord = { key: "k".repeat(64), item_id: "article", reference: { item_id: "article", title: item.title, source: "Journal", url: item.link },
  state: "ready", verified_bytes: 100, error_code: "", current: { job_id: "a".repeat(32), downloaded_at: 1800000000, partial: false, size_bytes: 200 } };
const body: OfflineBody = { item_id: "article", title: item.title, source: "Journal", url: item.link, text: "The local offline body.", truncated: false,
  images_omitted: false, source_mode: "public_web", job_id: "a".repeat(32), downloaded_at: 1800000000, partial: true,
  images: [{ index: 0, alt: "Missing chart", state: "missing", error_code: "offline_source_unreachable" }] };
let snapshot: OfflineStatus;
let client: ReturnType<typeof makeFixtureNodeClient>;
let confirm: ReturnType<typeof vi.fn>;
beforeEach(() => {
  snapshot = { records: [], used_bytes: 0, download_bytes: 0, limits: { item_bytes: 16777216, total_bytes: 268435456, image_bytes: 2097152, image_count: 8 } };
  client = { ...makeFixtureNodeClient(), mode: "live" };
  confirm = vi.fn(); context.value = { client, confirm };
  vi.spyOn(offlineApi, "status").mockImplementation(async () => snapshot);
  vi.spyOn(offlineApi, "download").mockImplementation(async () => { snapshot = { ...snapshot, records: [{ ...record, state: "queued", current: null }] }; return snapshot.records[0]; });
  vi.spyOn(offlineApi, "body").mockResolvedValue(body);
  vi.spyOn(offlineApi, "image").mockRejectedValue(new Error("Should not fetch a missing image"));
  vi.spyOn(offlineApi, "review").mockResolvedValue({ review_token: "r".repeat(64), copies: 1, bytes: 200, pending: 0 });
  vi.spyOn(offlineApi, "clear").mockResolvedValue({ review_token: "r".repeat(64), copies: 1, bytes: 200, pending: 0, freed_bytes: 200 });
  vi.spyOn(digestApi, "listConsumption").mockResolvedValue([saved]);
  vi.spyOn(digestApi, "readArticle").mockRejectedValue(new Error("External source forbidden"));
  vi.spyOn(client, "getContentBody").mockRejectedValue(new Error("Fallback forbidden"));
  vi.spyOn(client, "recordContentConsumption").mockResolvedValue(undefined);
});
afterEach(() => vi.restoreAllMocks());
function mount() { const view = render(<MemoryRouter><OfflineReading /></MemoryRouter>); return { ...view, user: userEvent.setup() }; }

it("does not download ordinary bookmarks and does not call a queued task available", async () => {
  const { user } = mount();
  await screen.findByText(/No downloads in this view/);
  expect(offlineApi.download).not.toHaveBeenCalled();
  await user.selectOptions(screen.getByLabelText("Content to download"), "article");
  await user.click(screen.getByRole("button", { name: "Download for offline" }));
  expect(await screen.findByText(/Queued ·/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Read offline copy" })).not.toBeInTheDocument();
  expect(offlineApi.download).toHaveBeenCalledExactlyOnceWith("article");
});

it("shows cancellation as requested until the node confirms it, keeping the old copy", async () => {
  snapshot.records = [{ ...record, state: "downloading" }];
  vi.spyOn(offlineApi, "cancel").mockImplementation(async () => { snapshot.records = [{ ...record, state: "cancel_requested" }]; return snapshot.records[0]; });
  const { user } = mount();
  await user.click(await screen.findByRole("button", { name: "Cancel download" }));
  expect(await screen.findByText(/Cancellation requested ·/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Cancel download" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Read offline copy" })).toBeEnabled();
  expect(screen.getByText(/Previous copy is still available/)).toBeInTheDocument();
});

it("reviews counts and space before clearing, and reports a stale review without success", async () => {
  snapshot.records = [record];
  vi.mocked(offlineApi.clear).mockRejectedValue(new Error("Downloads changed after your review. Review again."));
  const { user } = mount();
  await user.click(await screen.findByRole("button", { name: "Clear this download" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  const review = confirm.mock.calls[0][0] as ConfirmRequest;
  expect(review.body).toContain("1 saved copies"); expect(review.body).toContain("200 B");
  expect(review.body).toContain("Bookmarks, reading positions, conversations");
  expect(offlineApi.clear).not.toHaveBeenCalled();
  await act(async () => { await review.onConfirm(); });
  expect(offlineApi.clear).toHaveBeenCalledWith("r".repeat(64), "article");
  expect(await screen.findByRole("alert")).toHaveTextContent("changed after your review");
  expect(screen.queryByText(/freed\./)).not.toBeInTheDocument();
});

it("reads local text and partial images, restores progress and visits the source only on click", async () => {
  snapshot.records = [{ ...record, state: "partial" }];
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(1500);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(500);
  const open = vi.spyOn(window, "open").mockReturnValue(null);
  const { user, container } = mount();
  await user.click(await screen.findByRole("button", { name: "Read offline copy" }));
  const stage = container.querySelector(".content-viewer-stage") as HTMLElement;
  const dialog = await screen.findByRole("dialog", { name: item.title });
  expect(await within(dialog).findByText(body.text)).toBeInTheDocument();
  expect(within(dialog).getByText(/Image unavailable.*Missing chart/)).toBeInTheDocument();
  await waitFor(() => expect(stage.scrollTop).toBe(400));
  expect(digestApi.readArticle).not.toHaveBeenCalled(); expect(client.getContentBody).not.toHaveBeenCalled();
  expect(offlineApi.image).not.toHaveBeenCalled(); expect(open).not.toHaveBeenCalled();
  expect(dialog.querySelector("iframe, video, audio, img")).toBeNull();
  await user.click(within(dialog).getByRole("button", { name: "Read original" }));
  expect(open).toHaveBeenCalledWith(item.link, "_blank", "noopener,noreferrer");
  stage.scrollTop = 650; fireEvent.scroll(stage);
  await waitFor(() => expect(client.recordContentConsumption).toHaveBeenCalledWith(expect.anything(), "progress", 0.65));
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "Read offline copy" })).toHaveFocus();
});

it("never silently fetches the web after an offline read fails, and retries locally", async () => {
  snapshot.records = [record];
  vi.mocked(offlineApi.body).mockRejectedValueOnce(new Error("The saved copy failed verification."));
  const { user } = mount();
  await user.click(await screen.findByRole("button", { name: "Read offline copy" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("failed verification");
  expect(digestApi.readArticle).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Retry reading" }));
  expect(await screen.findByText(body.text)).toBeInTheDocument();
  expect(offlineApi.body).toHaveBeenCalledTimes(2);
  expect(digestApi.readArticle).not.toHaveBeenCalled();
});

it("restores unfinished file cleanup after remount and retries its original identity", async () => {
  snapshot.records = [{ ...record, state: 'cleared', current: null, verified_bytes: 0 }];
  snapshot.cleanup = { review_token: 'c'.repeat(64), item_id: 'article', sequence: 7, done: false, copies: 1, bytes: 200 };
  vi.mocked(offlineApi.clear).mockImplementation(async () => {
    snapshot = { ...snapshot, cleanup: { ...snapshot.cleanup!, done: true } };
    return { review_token: 'c'.repeat(64), copies: 1, bytes: 200, pending: 0, freed_bytes: 200 };
  });
  const initial = mount();
  await screen.findByRole('heading', { name: 'File cleanup unfinished' });
  initial.unmount();
  const { user } = mount();
  expect(await screen.findByText(/Removed from reader; file cleanup unfinished/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Clear all downloads' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Continue file cleanup' }));
  expect(offlineApi.clear).toHaveBeenCalledExactlyOnceWith('c'.repeat(64), 'article');
  expect(screen.queryByRole('heading', { name: 'File cleanup unfinished' })).not.toBeInTheDocument();
  expect(screen.getByText(/freed across this cleanup/)).toHaveFocus();
});

it("requires a fresh confirmation to clear changed remaining files", async () => {
  snapshot.cleanup = { review_token: 'c'.repeat(64), item_id: null, sequence: 7, done: false, copies: 1, bytes: 200 };
  vi.spyOn(offlineApi, 'reviewRemaining').mockResolvedValue({ review_token: 'new-review', files: 1, bytes: 350 });
  const approve = vi.spyOn(offlineApi, 'clearRemaining').mockImplementation(async () => {
    snapshot = { ...snapshot, cleanup: { ...snapshot.cleanup!, done: true } };
    return { review_token: 'c'.repeat(64), copies: 1, bytes: 350, pending: 0, freed_bytes: 350 };
  });
  const { user } = mount();
  await user.click(await screen.findByRole('button', { name: 'Review remaining file versions' }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  expect(confirm.mock.calls[0][0].body).toContain('1 remaining files, 350 B');
  expect(approve).not.toHaveBeenCalled();
  await act(async () => { await confirm.mock.calls[0][0].onConfirm(); });
  expect(approve).toHaveBeenCalledExactlyOnceWith('new-review');
  expect(screen.getByText(/350 B freed across this cleanup/)).toBeInTheDocument();
});

it("retains the request identity if its response is lost before progress can be read", async () => {
  snapshot.records = [record];
  vi.mocked(offlineApi.clear).mockRejectedValueOnce(new Error('Response lost'));
  const { user } = mount();
  await user.click(await screen.findByRole('button', { name: 'Clear this download' }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  await act(async () => { await confirm.mock.calls[0][0].onConfirm(); });
  expect(screen.getByRole('alert')).toHaveTextContent('Response lost');
  expect(screen.getByRole('button', { name: 'Clear all downloads' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Retry same cleanup request' }));
  expect(vi.mocked(offlineApi.clear).mock.calls).toEqual([['r'.repeat(64), 'article'], ['r'.repeat(64), 'article']]);
  expect(screen.getByText(/200 B freed across this cleanup/)).toBeInTheDocument();
});
