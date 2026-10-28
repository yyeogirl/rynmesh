import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { CleanupError, conversationCleanup, type CleanupJob, type CleanupReview } from "../../domain/conversationCleanup";
import ConversationCleanupPanel from "./ConversationCleanupPanel";

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../../appContext", () => ({ useAppContext: () => ({ confirm }) }));
afterEach(() => { vi.restoreAllMocks(); confirm.mockReset(); });
const review: CleanupReview = { review_token: "a".repeat(64), conversations: 2, recovery_items: 1, identities: 3,
  has_unassigned_draft: true, active_tasks: 0, backup_files: 2, backup_bytes: 1024, order_results: 1 };
const finished: CleanupJob = { id: review.review_token, done: ["source", "replica", "backups", "search", "orders"], pending: [],
  cancelled: false, local_copies_complete: true, browser_cleanup_required: true, remote_confirmed: false, identities: 3 };
function empty() {
  vi.spyOn(conversationCleanup, "jobs").mockResolvedValue([]);
  vi.spyOn(conversationCleanup, "preview").mockResolvedValue(review);
}

it("reviews scope and only clears after explicit confirmation", async () => {
  empty();
  const begin = vi.spyOn(conversationCleanup, "begin").mockResolvedValue(finished);
  const user = userEvent.setup();
  render(<ConversationCleanupPanel />);
  await screen.findByText("No previous conversation cleanup.");
  await user.click(screen.getByRole("button", { name: "Review conversation data" }));
  expect(await screen.findByText("Your unsent Ask Ryn draft is included.")).toBeInTheDocument();
  expect(screen.getByLabelText('Reviewed conversation scope')).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "Clear reviewed node copies" }));
  expect(begin).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("Conversations: 2");
  expect(confirm.mock.calls[0][0].body).toContain("Browser copies and remote devices need separate confirmation");
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(begin).toHaveBeenCalledExactlyOnceWith(review.review_token);
  expect(screen.getByRole("status")).toHaveTextContent("remote devices remain unconfirmed");
  expect(screen.getByRole("status")).toHaveFocus();
});

it("keeps a stable request identity when the response is lost", async () => {
  empty();
  const begin = vi.spyOn(conversationCleanup, "begin").mockRejectedValueOnce(new CleanupError("unconfirmed", "The node did not confirm this operation."))
    .mockResolvedValue(finished);
  const user = userEvent.setup();
  render(<ConversationCleanupPanel />);
  await screen.findByText("No previous conversation cleanup.");
  await user.click(screen.getByRole("button", { name: "Review conversation data" }));
  await user.click(await screen.findByRole("button", { name: "Clear reviewed node copies" }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.getByRole("alert")).toHaveTextContent("did not confirm");
  expect(screen.getByRole("alert")).toHaveFocus();
  expect(screen.queryByText("Reviewed node copies cleared")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Retry same cleanup request" }));
  expect(begin.mock.calls.map(([token]) => token)).toEqual([review.review_token, review.review_token]);
});

it("shows resumed pending steps after remount and does not start another cleanup", async () => {
  const partial = { ...finished, done: ["source", "replica"] as CleanupJob["done"], pending: ["backups", "search", "orders"] as CleanupJob["pending"], local_copies_complete: false };
  vi.spyOn(conversationCleanup, "jobs").mockResolvedValue([partial]);
  const resume = vi.spyOn(conversationCleanup, "resume").mockResolvedValue(finished);
  const begin = vi.spyOn(conversationCleanup, "begin");
  const user = userEvent.setup();
  const first = render(<ConversationCleanupPanel />);
  await screen.findByText("Remaining: Reviewed backups, Search index, Saved AI results");
  first.unmount();
  render(<ConversationCleanupPanel />);
  await screen.findByText("Remaining: Reviewed backups, Search index, Saved AI results");
  expect(screen.getByRole("button", { name: "Review conversation data" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Continue cleanup 1" }));
  expect(resume).toHaveBeenCalledExactlyOnceWith(finished.id);
  expect(begin).not.toHaveBeenCalled();
});

it("requires current backup review before clearing a changed remainder", async () => {
  vi.spyOn(conversationCleanup, "jobs").mockResolvedValue([{ ...finished, done: ["source", "replica"], pending: ["backups", "search", "orders"], local_copies_complete: false }]);
  vi.spyOn(conversationCleanup, "reviewBackups").mockResolvedValue({ review_token: "backup-review", files: 1, bytes: 2048 });
  const approve = vi.spyOn(conversationCleanup, "approveBackups").mockResolvedValue(finished);
  const user = userEvent.setup();
  render(<ConversationCleanupPanel />);
  await user.click(await screen.findByRole("button", { name: "Review remaining backups" }));
  expect(approve).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("Files: 1. Size: 2.0 KiB");
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(approve).toHaveBeenCalledExactlyOnceWith(finished.id, "backup-review");
});

it("requires waiting for active AI tasks and discards a stale review", async () => {
  empty();
  vi.mocked(conversationCleanup.preview).mockResolvedValueOnce({ ...review, active_tasks: 1 }).mockResolvedValue(review);
  vi.spyOn(conversationCleanup, "begin").mockRejectedValue(new CleanupError("ask_privacy_review_changed", "Your conversations changed after review."));
  const user = userEvent.setup();
  render(<ConversationCleanupPanel />);
  await screen.findByText("No previous conversation cleanup.");
  await user.click(screen.getByRole("button", { name: "Review conversation data" }));
  expect(await screen.findByRole("button", { name: "Clear reviewed node copies" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Review conversation data" }));
  await user.click(screen.getByRole("button", { name: "Clear reviewed node copies" }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.queryByRole("button", { name: "Clear reviewed node copies" })).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("changed after review");
});

it("keeps the operation number after older history leaves the list", async () => {
  const partial: CleanupJob = { ...finished, sequence: 35, done: ["source", "replica"],
    pending: ["backups", "search", "orders"], local_copies_complete: false };
  vi.spyOn(conversationCleanup, "jobs").mockResolvedValue([partial, { ...finished, id: "older", sequence: 34 }]);
  const resume = vi.spyOn(conversationCleanup, "resume").mockResolvedValue({ ...finished, sequence: 35 });
  const user = userEvent.setup();
  render(<ConversationCleanupPanel />);
  await screen.findByRole("heading", { name: "Cleanup 35" });
  expect(screen.getByText(/most recent 32 cleanup records/)).toHaveTextContent("deletion markers remain");
  await user.click(screen.getByRole("button", { name: "Continue cleanup 35" }));
  expect(resume).toHaveBeenCalledExactlyOnceWith(partial.id);
  expect(screen.getByRole("heading", { name: "Cleanup 35" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Cleanup 34" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Cleanup 2" })).not.toBeInTheDocument();
});
