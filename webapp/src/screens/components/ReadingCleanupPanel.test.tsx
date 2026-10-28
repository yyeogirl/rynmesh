import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ReadingCleanupError, readingCleanup, type ReadingCleanupJob, type ReadingCleanupReview } from "../../domain/readingCleanup";
import ReadingCleanupPanel from "./ReadingCleanupPanel";

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../../appContext", () => ({ useAppContext: () => ({ confirm }) }));
afterEach(() => { vi.restoreAllMocks(); confirm.mockReset(); });
const review: ReadingCleanupReview = { review_token: "a".repeat(64), local_items: 2, source_entities: 4, replica_entities: 4, backup_files: 2 };
const finished: ReadingCleanupJob = { id: review.review_token, sequence: 1, done: ["source", "replica", "backups", "search"], pending: [], cancelled: false, local_copies_complete: true, remote_confirmed: false };
function empty() {
  vi.spyOn(readingCleanup, "status").mockResolvedValue(null);
  vi.spyOn(readingCleanup, "preview").mockResolvedValue(review);
}

it("reviews explicit scope and confirms before clearing", async () => {
  empty();
  const begin = vi.spyOn(readingCleanup, "begin").mockResolvedValue(finished);
  const onChange = vi.fn();
  const user = userEvent.setup();
  render(<ReadingCleanupPanel onChange={onChange} />);
  await screen.findByText("No previous reading cleanup.");
  await user.click(screen.getByRole("button", { name: "Review reading data" }));
  expect(await screen.findByLabelText("Reviewed reading scope")).toHaveFocus();
  await user.tab();
  await user.keyboard("{Enter}");
  expect(begin).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("Saved documents, downloaded articles, browser copies and other devices are outside");
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(begin).toHaveBeenCalledExactlyOnceWith(review.review_token);
  expect(onChange).toHaveBeenCalledOnce();
  expect(screen.getByRole("status")).toHaveFocus();
  expect(screen.getByRole("status")).toHaveTextContent("Other devices and saved or downloaded content remain outside");
});

it("preserves original request identity when the node does not confirm it", async () => {
  empty();
  const begin = vi.spyOn(readingCleanup, "begin").mockRejectedValueOnce(new ReadingCleanupError("unconfirmed", "No confirmation"))
    .mockResolvedValueOnce(finished);
  const user = userEvent.setup();
  render(<ReadingCleanupPanel />);
  await screen.findByText("No previous reading cleanup.");
  await user.click(screen.getByRole("button", { name: "Review reading data" }));
  await user.click(await screen.findByRole("button", { name: "Clear reviewed reading copies" }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.getByRole("alert")).toHaveTextContent("No confirmation");
  expect(screen.getByRole("button", { name: "Review reading data" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Retry original reading cleanup" }));
  expect(begin.mock.calls).toEqual([[review.review_token], [review.review_token]]);
  expect(await screen.findByText("Reviewed local reading copies cleared")).toBeInTheDocument();
});

it("restores incomplete progress and reviews changed backups before retrying", async () => {
  vi.spyOn(readingCleanup, "status").mockResolvedValue({ ...finished, done: ["source", "replica"], pending: ["backups", "search"], local_copies_complete: false });
  vi.spyOn(readingCleanup, "reviewBackups").mockResolvedValue({ review_token: "backup", files: 1, bytes: 2048 });
  const approve = vi.spyOn(readingCleanup, "approveBackups").mockResolvedValue(finished);
  const user = userEvent.setup();
  render(<ReadingCleanupPanel />);
  await user.click(await screen.findByRole("button", { name: "Review remaining reading backups" }));
  expect(screen.getByRole("button", { name: "Review reading data" })).toBeDisabled();
  expect(confirm.mock.calls[0][0].body).toContain("Files: 1. Size: 2.0 KiB");
  expect(approve).not.toHaveBeenCalled();
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(approve).toHaveBeenCalledExactlyOnceWith(finished.id, "backup");
});

it("discards stale scope instead of repeatedly clearing against an old review", async () => {
  empty();
  vi.spyOn(readingCleanup, "begin").mockRejectedValue(new ReadingCleanupError("reading_privacy_review_changed", "Review changed"));
  const user = userEvent.setup();
  render(<ReadingCleanupPanel />);
  await screen.findByText("No previous reading cleanup.");
  await user.click(screen.getByRole("button", { name: "Review reading data" }));
  await user.click(await screen.findByRole("button", { name: "Clear reviewed reading copies" }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.queryByLabelText("Reviewed reading scope")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry original reading cleanup" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Review reading data" })).toBeEnabled();
});

it("keeps review disabled if initial progress cannot be loaded", async () => {
  vi.spyOn(readingCleanup, "status").mockRejectedValue(new Error("Node unavailable"));
  render(<ReadingCleanupPanel />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Node unavailable");
  expect(screen.getByRole("button", { name: "Review reading data" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Refresh reading cleanup" })).toBeEnabled();
});
